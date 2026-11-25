"""Navidrome library checks and playlist publishing using the Subsonic API."""

import hashlib
import re
import secrets
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import requests


class NavidromeError(Exception):
    """A library or playlist operation could not be completed reliably."""


def normalize_track_text(value: str) -> str:
    """Ignore capitalization, accents, and punctuation, but retain version names."""

    text = unicodedata.normalize("NFKD", value.casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[\W_]+", " ", text).strip()


@dataclass(frozen=True)
class Navidrome:
    """A connection whose credentials are never put into request URLs."""

    url: str
    username: str
    password: str = field(repr=False)

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or "@" in parsed.netloc
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Navidrome URL must be an HTTP(S) server URL.")
        if not self.username.strip() or not self.password:
            raise ValueError("Navidrome username and password are required.")

    def request(self, method: str, **parameters: Any) -> dict[str, Any]:
        """Make an authenticated request, failing closed on invalid responses."""

        salt = secrets.token_hex(16)
        token = hashlib.md5(
            (self.password + salt).encode(), usedforsecurity=False
        ).hexdigest()
        try:
            response = requests.post(
                f"{self.url.rstrip('/')}/rest/{method}.view",
                data={
                    "u": self.username,
                    "t": token,
                    "s": salt,
                    "v": "1.16.1",
                    "c": "spotdl-radio",
                    "f": "json",
                    **parameters,
                },
                timeout=15,
                allow_redirects=False,
            )
        except requests.RequestException as exception:
            raise NavidromeError(
                f"Navidrome connection failed ({type(exception).__name__})."
            ) from exception
        if response.status_code != 200:
            raise NavidromeError(
                f"Navidrome returned HTTP {response.status_code}; check its URL and availability."
            )
        try:
            payload = response.json()
        except ValueError as exception:
            raise NavidromeError("Navidrome returned invalid JSON.") from exception
        result = payload.get("subsonic-response") if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            raise NavidromeError("Navidrome returned an invalid Subsonic response.")
        if result.get("status") != "ok":
            error = result.get("error") or {}
            if not isinstance(error, dict):
                raise NavidromeError("Navidrome returned an invalid error response.")
            raise NavidromeError(
                f"Navidrome rejected the request (code {error.get('code', 'unknown')}). "
                "Check the account and its library permissions."
            )
        return result

    def ping(self) -> None:
        """Verify credentials before enabling monitoring."""

        self.request("ping")

    def contains(self, artist: str, title: str) -> bool:
        """Check whether a matching recording is in the library."""

        return self.find_song_id(artist, title) is not None

    def find_song_id(self, artist: str, title: str) -> str | None:
        """Check every search page, requiring both the title and artist to match."""

        offset = 0
        while True:
            result = self.request(
                "search3",
                query=f"{artist} {title}",
                artistCount=0,
                albumCount=0,
                songCount=100,
                songOffset=offset,
            )
            search = result.get("searchResult3")
            if not isinstance(search, dict):
                raise NavidromeError("Navidrome omitted the song search result.")
            songs = search.get("song", [])
            if not isinstance(songs, list):
                raise NavidromeError("Navidrome returned an invalid song list.")
            for song in songs:
                if not isinstance(song, dict) or not all(
                    isinstance(song.get(key), str) for key in ("artist", "title")
                ):
                    raise NavidromeError("Navidrome returned incomplete song metadata.")
                artists = [song["artist"]]
                artists.extend(
                    item["name"]
                    for item in (song.get("artists") or [])
                    if isinstance(item, dict) and isinstance(item.get("name"), str)
                )
                if normalize_track_text(song["title"]) == normalize_track_text(
                    title
                ) and normalize_track_text(artist) in {
                    normalize_track_text(name) for name in artists
                }:
                    song_id = song.get("id")
                    if not isinstance(song_id, str) or not song_id:
                        raise NavidromeError("Navidrome omitted a matching song's ID.")
                    return song_id
            if len(songs) < 100:
                return None
            offset += len(songs)

    def search_songs(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Return validated Navidrome song results for a user-facing search."""

        result = self.request(
            "search3",
            query=query,
            artistCount=0,
            albumCount=0,
            songCount=limit,
            songOffset=0,
        )
        search = result.get("searchResult3")
        if not isinstance(search, dict):
            raise NavidromeError("Navidrome omitted its song search results.")
        songs = search.get("song", [])
        if not isinstance(songs, list):
            raise NavidromeError("Navidrome returned an invalid song list.")
        validated = []
        for song in songs:
            if not isinstance(song, dict) or not all(
                isinstance(song.get(key), str) and song[key]
                for key in ("id", "title", "artist")
            ):
                raise NavidromeError("Navidrome returned incomplete song metadata.")
            validated.append(
                {
                    "id": song["id"],
                    "title": song["title"],
                    "artist": song["artist"],
                    "album": song.get("album", ""),
                    "duration": song.get("duration"),
                }
            )
        return validated

    def get_song(self, song_id: str) -> dict[str, Any]:
        """Fetch a song by ID, including its Navidrome-relative file path."""

        result = self.request("getSong", id=song_id)
        song = result.get("song")
        if (
            not isinstance(song, dict)
            or song.get("id") != song_id
            or not isinstance(song.get("title"), str)
            or not song["title"]
            or not isinstance(song.get("artist"), str)
            or not song["artist"]
            or not isinstance(song.get("path"), str)
            or not song["path"]
        ):
            raise NavidromeError("Navidrome returned incomplete song file metadata.")
        return song

    def replace_playlist(
        self, name: str, song_ids: list[str], *, previous_name: str | None = None
    ) -> str:
        """Replace an owned, named playlist's entire contents, or create it once."""

        result = self.request("getPlaylists")
        container = result.get("playlists")
        if not isinstance(container, dict):
            raise NavidromeError("Navidrome omitted its playlists.")
        playlists = container.get("playlist", [])
        if not isinstance(playlists, list):
            raise NavidromeError("Navidrome returned an invalid playlist list.")
        matches = []
        previous_matches = []
        for playlist in playlists:
            if not isinstance(playlist, dict) or not all(
                isinstance(playlist.get(key), str) and playlist[key]
                for key in ("id", "name", "owner")
            ):
                raise NavidromeError("Navidrome returned invalid playlist metadata.")
            if playlist.get("name") == name and playlist.get("owner") == self.username:
                matches.append(playlist["id"])
            elif (
                playlist["name"] == previous_name and playlist["owner"] == self.username
            ):
                previous_matches.append(playlist["id"])
        rename = not matches and bool(previous_matches)
        if rename:
            matches = previous_matches
        if len(matches) > 1:
            raise NavidromeError(
                f"Multiple owned playlists named {(previous_name if rename else name)!r}; keep only one."
            )
        parameters: dict[str, Any] = {"songId": list(dict.fromkeys(song_ids))}
        if matches:
            parameters["playlistId"] = matches[0]
        else:
            parameters["name"] = name
        result = self.request("createPlaylist", **parameters)
        created = result.get("playlist")
        if (
            not isinstance(created, dict)
            or not isinstance(created.get("id"), str)
            or not created["id"]
        ):
            raise NavidromeError("Navidrome omitted the published playlist ID.")
        if matches and created["id"] != matches[0]:
            raise NavidromeError("Navidrome returned a different playlist ID.")
        if rename:
            self.request("updatePlaylist", playlistId=created["id"], name=name)
        return created["id"]
