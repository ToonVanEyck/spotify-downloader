"""Persistent radio history polling and library-aware download processing."""

import asyncio
import hashlib
import logging
import os
import re
import sqlite3
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from rapidfuzz.fuzz import ratio

from spotdl.download.downloader import Downloader
from spotdl.types.options import DownloaderOptions
from spotdl.types.song import Song
from spotdl.utils.logging import capture_web_output
from spotdl.utils.navidrome import Navidrome, normalize_track_text
from spotdl.utils.search import get_search_results
from spotdl.utils.web import get_web_download_output

logger = logging.getLogger(__name__)

POLL_INTERVAL = 10 * 60
DOWNLOAD_RETRIES = 5
DOWNLOAD_RETRY_INTERVAL = 2 * 60
STATIONS = {
    "radio1": "Radio 1",
    "radio2": "Radio 2",
    "radio2-benebene": "Radio 2 Bene",
    "stubru": "Studio Brussel",
    "stubru-tijdloze": "De Tijdloze",
    "mnm": "MNM",
    "mnm-hits": "MNM Hits",
    "klara": "Klara",
    "klara-continuo": "Klara Continuo",
    "willy": "Willy",
    "willy-classic": "Willy Classic",
    "nostalgie": "Nostalgie",
}
WILLY_STATIONS = {"willy": "willy_be", "willy-classic": "willy_be_class_x"}
PLAYLIST_TIMEZONE = ZoneInfo("Europe/Brussels")
PLAYLIST_RETRY_INTERVAL = 5 * 60


class RadioError(Exception):
    """A source or download could not be processed safely."""


@dataclass(frozen=True)
class RadioPlay:
    """One timestamped broadcast, rather than a unique song."""

    played_at: str
    artist: str
    title: str
    spotify_url: str = ""
    youtube_url: str = ""

    @property
    def search_artist(self) -> str:
        """Klara appends performer notes to the composer's artist field."""

        return self.artist.split(" | Uitvoerder:", 1)[0].strip()

    @property
    def song_key(self) -> str:
        """Identify a recording across stations and repeated broadcasts."""

        return hashlib.sha256(
            (
                normalize_track_text(self.artist)
                + "\n"
                + normalize_track_text(self.title)
            ).encode()
        ).hexdigest()


def parse_radio_history(html: str) -> list[RadioPlay]:
    """Extract playable tracks, excluding news and other non-song broadcasts."""

    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one(".tracks") is None:
        raise RadioError(
            "Radio history markup was not found; the site may have changed."
        )
    plays = []
    for track in soup.select(".tracks .track"):
        artist = track.select_one(".artist")
        title = track.select_one(".title")
        if artist is None or title is None:
            continue
        artist_text, title_text = artist.get_text(strip=True), title.get_text(
            strip=True
        )
        if not artist_text or not title_text:
            continue
        timestamp = track.select_one("[data-playtime]")
        if timestamp is None:
            raise RadioError("A radio track is missing its broadcast timestamp.")
        played = datetime.fromisoformat(str(timestamp["data-playtime"]))
        if played.tzinfo is None:
            raise RadioError("A radio timestamp is missing its timezone.")
        spotify = track.select_one("[data-spotify-uri]")
        match = (
            re.fullmatch(
                r"spotify:track:([A-Za-z0-9]{22})", str(spotify["data-spotify-uri"])
            )
            if spotify is not None
            else None
        )
        plays.append(
            RadioPlay(
                played.astimezone(timezone.utc).isoformat(),
                artist_text,
                title_text,
                f"https://open.spotify.com/track/{match[1]}" if match else "",
            )
        )
    return plays


def parse_willy_history(data: object) -> list[RadioPlay]:
    """Extract Willy broadcasts and validated Spotify and YouTube links."""

    if not isinstance(data, dict) or not isinstance(data.get("played_tracks"), list):
        raise RadioError("Willy response is missing its played_tracks list.")
    plays = []
    for track in data["played_tracks"]:
        if not isinstance(track, dict):
            raise RadioError("Invalid Willy track data.")
        artist = track.get("artist")
        title = track.get("title")
        if not isinstance(artist, dict):
            continue
        name = artist.get("name")
        if not isinstance(name, str) or not isinstance(title, str):
            continue
        if not name.strip() or not title.strip():
            continue
        timestamp = track.get("played_at")
        if not isinstance(timestamp, str):
            raise RadioError("A Willy track is missing its broadcast timestamp.")
        played = datetime.fromisoformat(timestamp)
        if played.tzinfo is None:
            raise RadioError("A Willy timestamp is missing its timezone.")
        spotify = track.get("spotify_url")
        match = (
            re.fullmatch(
                r"https?://open\.spotify\.com/track/([A-Za-z0-9]{22})", spotify
            )
            if isinstance(spotify, str)
            else None
        )
        videos = track.get("videos") or []
        if not isinstance(videos, list):
            raise RadioError("Invalid Willy video data.")
        youtube_id = next(
            (
                video["id"]
                for video in videos
                if isinstance(video, dict)
                and video.get("type") == "youtube"
                and isinstance(video.get("id"), str)
                and re.fullmatch(r"[A-Za-z0-9_-]{11}", video["id"])
            ),
            "",
        )
        plays.append(
            RadioPlay(
                played.astimezone(timezone.utc).isoformat(),
                name.strip(),
                title.strip(),
                f"https://open.spotify.com/track/{match[1]}" if match else "",
                f"https://www.youtube.com/watch?v={youtube_id}" if youtube_id else "",
            )
        )
    return plays


def parse_nostalgie_history(data: object) -> list[RadioPlay]:
    """Extract Nostalgie broadcasts, whose timestamps use Brussels local time."""

    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise RadioError("Nostalgie response is missing its data list.")
    plays = []
    for track in data["data"]:
        if not isinstance(track, dict):
            raise RadioError("Invalid Nostalgie track data.")
        artist, title = track.get("artist"), track.get("title")
        if not isinstance(artist, str) or not isinstance(title, str):
            continue
        if not artist.strip() or not title.strip():
            continue
        timestamp = track.get("played_at")
        if not isinstance(timestamp, str):
            raise RadioError("A Nostalgie track is missing its broadcast timestamp.")
        try:
            played = datetime.fromisoformat(timestamp)
        except ValueError as exception:
            raise RadioError("Invalid Nostalgie broadcast timestamp.") from exception
        if played.tzinfo is None:
            local = played.replace(tzinfo=PLAYLIST_TIMEZONE)
            if (
                local.astimezone(timezone.utc)
                .astimezone(PLAYLIST_TIMEZONE)
                .replace(tzinfo=None)
                != played
            ):
                raise RadioError("Invalid Nostalgie local broadcast timestamp.")
            played = local
        plays.append(
            RadioPlay(
                played.astimezone(timezone.utc).isoformat(),
                artist.strip(),
                title.strip(),
            )
        )
    return plays


def station_url(station: str) -> str:
    """Return a fixed, supported history source URL."""

    if station not in STATIONS:
        raise ValueError("Unknown radio station.")
    if station in WILLY_STATIONS:
        return (
            "https://api.willy.radio/2.0/tracks/plays?limit=30"
            f"&_station_id={WILLY_STATIONS[station]}"
        )
    if station == "nostalgie":
        return "https://mediahuis.pointbreak.dev/api/v1/radio/9/tracks-paginated"
    return f"https://nuopderadio.be/{station}/"


def fetch_radio_history(station: str) -> list[RadioPlay]:
    """Fetch a supported station's HTML or JSON history."""

    response = requests.get(
        station_url(station),
        headers={"User-Agent": "spotdl-radio-monitor"},
        timeout=20,
    )
    response.raise_for_status()
    if station in WILLY_STATIONS:
        return parse_willy_history(response.json())
    if station == "nostalgie":
        return parse_nostalgie_history(response.json())
    return parse_radio_history(response.text)


def playlist_window(now: datetime) -> tuple[datetime, datetime, datetime]:
    """Return previous, latest, and next local 03:00 boundaries as UTC."""

    boundary = now.astimezone(PLAYLIST_TIMEZONE).replace(
        hour=3, minute=0, second=0, microsecond=0
    )
    if boundary > now:
        boundary -= timedelta(days=1)
    return (
        (boundary - timedelta(days=1)).astimezone(timezone.utc),
        boundary.astimezone(timezone.utc),
        (boundary + timedelta(days=1)).astimezone(timezone.utc),
    )


class RadioMonitor:
    """Keep polling and downloading independently of browser/client sessions."""

    def __init__(
        self, database: Path, settings: DownloaderOptions, use_output_dir: bool = False
    ):
        database.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(database, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        database.chmod(0o600)
        self.db = sqlite3.connect(database)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                url TEXT NOT NULL, username TEXT NOT NULL, password TEXT NOT NULL
            );
            INSERT OR IGNORE INTO settings VALUES (1, '', '', '');
            CREATE TABLE IF NOT EXISTS stations (
                id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
                initialized INTEGER NOT NULL DEFAULT 0,
                checked_at REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS plays (
                id INTEGER PRIMARY KEY, station TEXT NOT NULL,
                played_at TEXT NOT NULL, artist TEXT NOT NULL, title TEXT NOT NULL,
                spotify_url TEXT NOT NULL, song_key TEXT NOT NULL,
                status TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
                path TEXT NOT NULL DEFAULT '',
                UNIQUE(station, played_at, artist, title)
            );
            CREATE TABLE IF NOT EXISTS collected (
                song_key TEXT PRIMARY KEY, path TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS playlist_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                next_run REAL NOT NULL DEFAULT 0,
                last_run TEXT NOT NULL DEFAULT '',
                attempted_at REAL NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT ''
            );
            INSERT OR IGNORE INTO playlist_state(id) VALUES (1);
            CREATE INDEX IF NOT EXISTS plays_status ON plays(status);
            """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(plays)")}
        if "youtube_url" not in columns:
            self.db.execute(
                "ALTER TABLE plays ADD COLUMN youtube_url TEXT NOT NULL DEFAULT ''"
            )
        if "attempts" not in columns:
            self.db.execute(
                "ALTER TABLE plays ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
            )
            self.db.execute(
                "UPDATE plays SET status='retry', attempts=1 WHERE status='failed'"
            )
        self.db.execute(
            """UPDATE plays SET status=CASE WHEN attempts > 0 THEN 'retry'
            ELSE 'queued' END WHERE status IN ('baseline', 'checking', 'downloading')"""
        )
        if "retry_at" not in columns:
            self.db.execute(
                "ALTER TABLE plays ADD COLUMN retry_at REAL NOT NULL DEFAULT 0"
            )
            for row in self.db.execute(
                """SELECT id, attempts FROM plays
                WHERE status IN ('retry', 'failed') AND attempts BETWEEN 1 AND ?""",
                (DOWNLOAD_RETRIES,),
            ).fetchall():
                self.db.execute(
                    "UPDATE plays SET status='retry', retry_at=? WHERE id=?",
                    (
                        time.time()
                        + DOWNLOAD_RETRY_INTERVAL * 2 ** (row["attempts"] - 1),
                        row["id"],
                    ),
                )
        self.db.executemany(
            "INSERT OR IGNORE INTO stations(id) VALUES (?)",
            [(station,) for station in STATIONS],
        )
        self.db.commit()
        self.settings = settings.copy()
        self.settings["simple_tui"] = True
        self.settings["output"] = get_web_download_output(settings, use_output_dir)
        self.downloader: Optional[Downloader] = None
        self.logs: deque[str] = deque(maxlen=1000)
        self.log_lock = Lock()
        self.wake = asyncio.Event()
        self.queue_wake = asyncio.Event()
        self.tasks: list[asyncio.Task] = []

    def append_log(self, message: str) -> None:
        """Keep a bounded terminal transcript without mixing client output."""

        with self.log_lock:
            self.logs.extend(line[:8000] for line in message.splitlines())

    def view(self) -> dict:
        """Return public configuration and recent activity, never the password."""

        configuration = self.db.execute("SELECT * FROM settings WHERE id=1").fetchone()
        stations = []
        for row in self.db.execute("SELECT * FROM stations"):
            station = dict(row)
            station["name"] = STATIONS[row["id"]]
            station["url"] = station_url(row["id"])
            station["checked"] = (
                datetime.fromtimestamp(row["checked_at"], timezone.utc).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
                if row["checked_at"]
                else "Not checked yet"
            )
            station["next_check"] = (
                "When new-song queue empties, not before "
                + datetime.fromtimestamp(
                    row["checked_at"] + POLL_INTERVAL, timezone.utc
                ).strftime("%Y-%m-%d %H:%M UTC")
                if row["checked_at"]
                else "On enabling"
            )
            stations.append(station)
        with self.log_lock:
            output = "\n".join(self.logs)
        playlists = dict(
            self.db.execute("SELECT * FROM playlist_state WHERE id=1").fetchone()
        )
        next_run = (
            datetime.fromtimestamp(playlists["next_run"], timezone.utc)
            if playlists["next_run"]
            else playlist_window(datetime.now(timezone.utc))[2]
        )
        playlists["next_check"] = next_run.astimezone(PLAYLIST_TIMEZONE).strftime(
            "%Y-%m-%d %H:%M %Z"
        )
        plays = []
        for row in self.db.execute(
            "SELECT * FROM plays ORDER BY played_at DESC, id DESC LIMIT 300"
        ):
            play = dict(row)
            play["attempt_number"] = row["attempts"] + (row["status"] != "failed")
            play["retry_check"] = (
                datetime.fromtimestamp(row["retry_at"], timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S UTC"
                )
                if row["status"] == "retry" and row["retry_at"]
                else ""
            )
            plays.append(play)
        return {
            "stations": stations,
            "selected_stations": [
                station["id"] for station in stations if station["enabled"]
            ],
            "navidrome_url": configuration["url"],
            "navidrome_username": configuration["username"],
            "password_saved": bool(configuration["password"]),
            "output_directory": self.settings["output"],
            "plays": plays,
            "max_attempts": DOWNLOAD_RETRIES + 1,
            "output": output,
            "playlists": playlists,
        }

    def navidrome(self) -> Navidrome:
        """Read the current server-side credentials for each library check."""

        connection = self.optional_navidrome()
        if connection is None:
            raise ValueError("Configure Navidrome on the Radio monitor page.")
        return connection

    def optional_navidrome(self) -> Optional[Navidrome]:
        """Allow ordinary downloads without setup, but reject partial settings."""

        row = self.db.execute("SELECT * FROM settings WHERE id=1").fetchone()
        if not any(row[key] for key in ("url", "username", "password")):
            return None
        return Navidrome(row["url"], row["username"], row["password"])

    async def configure(
        self, stations: list[str], url: str, username: str, password: str
    ) -> None:
        """Validate a connection before atomically saving server-wide settings."""

        if set(stations) - STATIONS.keys():
            raise ValueError("Unknown radio station.")
        url, username = url.strip().rstrip("/"), username.strip()
        previous = self.db.execute("SELECT * FROM settings WHERE id=1").fetchone()
        if not password and url == previous["url"] and username == previous["username"]:
            password = previous["password"]
        changed_credentials = (url, username, password) != (
            previous["url"],
            previous["username"],
            previous["password"],
        )
        if stations or url or username or password:
            connection = Navidrome(url, username, password)
            if stations or changed_credentials:
                await asyncio.to_thread(connection.ping)
        with self.db:
            self.db.execute(
                "UPDATE settings SET url=?, username=?, password=? WHERE id=1",
                (url, username, password),
            )
            if changed_credentials:
                self.db.execute("""UPDATE playlist_state SET next_run=0, last_run='',
                    attempted_at=0, error='' WHERE id=1""")
            for station in STATIONS:
                enabled = int(station in stations)
                self.db.execute(
                    """UPDATE stations SET enabled=?,
                    initialized=CASE WHEN enabled=? THEN initialized ELSE 0 END
                    WHERE id=?""",
                    (enabled, enabled, station),
                )
        self.append_log(f"Monitoring settings saved: {len(set(stations))} station(s).")
        self.check_now()

    def check_now(self) -> None:
        """Recheck empty queues without bypassing station or retry rate limits."""

        self.wake.set()
        self.queue_wake.set()

    async def start(self) -> None:
        """Resume persisted polling and unfinished work when the server starts."""

        self.configure_from_environment()
        self.tasks = [
            asyncio.create_task(self.poll_loop(), name="radio-poller"),
            asyncio.create_task(self.download_loop(), name="radio-downloader"),
            asyncio.create_task(self.playlist_loop(), name="radio-playlists"),
        ]
        self.queue_wake.set()

    def configure_from_environment(self) -> None:
        """Apply complete environment credentials without changing station choices."""

        keys = ("NAVIDROME_URL", "NAVIDROME_USERNAME", "NAVIDROME_PASSWORD")
        if not any(key in os.environ for key in keys):
            return
        if not all(os.environ.get(key) for key in keys):
            raise ValueError(
                "Set NAVIDROME_URL, NAVIDROME_USERNAME and NAVIDROME_PASSWORD together."
            )
        connection = Navidrome(
            os.environ["NAVIDROME_URL"].strip().rstrip("/"),
            os.environ["NAVIDROME_USERNAME"].strip(),
            os.environ["NAVIDROME_PASSWORD"],
        )
        if connection != self.optional_navidrome():
            with self.db:
                self.db.execute(
                    "UPDATE settings SET url=?, username=?, password=? WHERE id=1",
                    (connection.url, connection.username, connection.password),
                )
                self.db.execute("""UPDATE playlist_state SET next_run=0, last_run='',
                    attempted_at=0, error='' WHERE id=1""")
        self.append_log("Navidrome connection loaded from environment variables.")

    async def stop(self) -> None:
        """Cancel owned tasks before closing persistent state."""

        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with suppress(asyncio.CancelledError):
                await task
        self.db.close()

    async def poll_station(self, station: str) -> None:
        """Queue unseen plays, including the first fetched history."""

        self.append_log(f"[{STATIONS[station]}] Fetching radio history...")
        plays = await asyncio.to_thread(fetch_radio_history, station)
        added = 0
        with self.db:
            for play in reversed(plays):
                cursor = self.db.execute(
                    """INSERT OR IGNORE INTO plays
                    (station, played_at, artist, title, spotify_url, song_key, status, youtube_url)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        station,
                        play.played_at,
                        play.artist,
                        play.title,
                        play.spotify_url,
                        play.song_key,
                        "queued",
                        play.youtube_url,
                    ),
                )
                added += cursor.rowcount
            self.db.execute(
                "UPDATE stations SET initialized=1, checked_at=?, error='' WHERE id=?",
                (time.time(), station),
            )
        self.append_log(f"[{STATIONS[station]}] {added} new play(s) queued.")
        self.queue_wake.set()

    async def poll_loop(self) -> None:
        """Refill when no first attempts remain, at most once per ten minutes."""

        while True:
            self.wake.clear()
            stations = self.db.execute(
                "SELECT * FROM stations WHERE enabled=1"
            ).fetchall()
            for station in stations:
                if (
                    station["checked_at"]
                    and station["checked_at"] + POLL_INTERVAL > time.time()
                ):
                    continue
                if self.db.execute(
                    """SELECT 1 FROM plays WHERE station=? AND attempts=0
                    AND status IN ('queued', 'checking', 'downloading') LIMIT 1""",
                    (station["id"],),
                ).fetchone():
                    continue
                try:
                    await self.poll_station(station["id"])
                except Exception as exception:  # pylint: disable=broad-exception-caught
                    logger.exception("Radio history fetch failed: %s", station["id"])
                    self.append_log(f"ERROR [{STATIONS[station['id']]}] {exception}")
                    with self.db:
                        self.db.execute(
                            "UPDATE stations SET checked_at=?, error=? WHERE id=?",
                            (time.time(), str(exception), station["id"]),
                        )
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    async def download(self, play: RadioPlay) -> Path:
        """Resolve radio metadata through spotDL and download the matching song."""

        song: Optional[Song]
        if play.spotify_url:
            song = await asyncio.to_thread(Song.from_url, play.spotify_url)
        else:
            songs = await asyncio.to_thread(
                get_search_results, f"{play.search_artist} {play.title}", limit=5
            )
            song = next(
                (
                    candidate
                    for candidate in songs
                    if ratio(
                        normalize_track_text(candidate.name or ""),
                        normalize_track_text(play.title),
                    )
                    >= 85
                    and ratio(
                        normalize_track_text(candidate.artist or ""),
                        normalize_track_text(play.search_artist),
                    )
                    >= 85
                ),
                None,
            )
            if song is None:
                raise RadioError("No matching Spotify recording found for this play.")
        if self.downloader is None:
            self.downloader = await asyncio.to_thread(
                Downloader, settings=self.settings, loop=asyncio.get_running_loop()
            )
        try:
            _, path = await self.downloader.pool_download(song)
            if path is None or not path.is_file():
                raise RadioError(
                    "spotDL did not produce an audio file; see terminal output."
                )
            return path
        finally:
            self.downloader.progress_handler.progress_tracker.songs.clear()
            self.downloader.errors.clear()

    def set_status(self, play_id: int, status: str, message: str = "", path: str = ""):
        """Persist each processing transition for the UI and restart recovery."""

        with self.db:
            self.db.execute(
                "UPDATE plays SET status=?, message=?, path=? WHERE id=?",
                (status, message, path, play_id),
            )
        self.wake.set()

    async def process_play(self, row: sqlite3.Row) -> None:
        """Never download after a failed library check, or collect a song twice."""

        play = RadioPlay(
            row["played_at"],
            row["artist"],
            row["title"],
            row["spotify_url"],
            row["youtube_url"],
        )
        label = f"[{STATIONS[row['station']]}] {play.artist} - {play.title}"
        self.set_status(row["id"], "checking")
        self.append_log(f"{label}: checking Navidrome...")
        try:
            collected = self.db.execute(
                "SELECT path FROM collected WHERE song_key=?", (play.song_key,)
            ).fetchone()
            local_copy = collected is not None and Path(collected["path"]).is_file()
            if local_copy or await asyncio.to_thread(
                self.navidrome().contains, play.search_artist, play.title
            ):
                self.set_status(row["id"], "already collected")
                self.append_log(f"{label}: already collected.")
                return
            enabled = self.db.execute(
                "SELECT enabled FROM stations WHERE id=?", (row["station"],)
            ).fetchone()["enabled"]
            if not enabled:
                self.set_status(
                    row["id"],
                    "retry" if row["attempts"] else "queued",
                    "Source paused.",
                )
                return
            self.set_status(row["id"], "downloading")
            with capture_web_output(self.append_log):
                path = await self.download(play)
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO collected VALUES (?, ?)",
                    (play.song_key, str(path.absolute())),
                )
            self.set_status(row["id"], "downloaded", path=str(path.absolute()))
            self.append_log(f"{label}: downloaded.")
        except asyncio.CancelledError:
            self.set_status(
                row["id"],
                "retry" if row["attempts"] else "queued",
                "Interrupted; will resume on restart.",
            )
            raise
        except Exception as exception:  # pylint: disable=broad-exception-caught
            logger.exception("Radio download failed for %s", label)
            attempts = row["attempts"] + 1
            status = "retry" if attempts <= DOWNLOAD_RETRIES else "failed"
            delay = DOWNLOAD_RETRY_INTERVAL * 2 ** (attempts - 1)
            retry_at = time.time() + delay if status == "retry" else 0
            with self.db:
                self.db.execute(
                    """UPDATE plays SET status=?, message=?, attempts=?, retry_at=?
                    WHERE id=?""",
                    (status, str(exception), attempts, retry_at, row["id"]),
                )
            action = (
                f"retry {attempts}/{DOWNLOAD_RETRIES} in {delay // 60} minutes"
                if status == "retry"
                else "giving up until replayed"
            )
            self.append_log(
                f"ERROR {label}: attempt {attempts}/{DOWNLOAD_RETRIES + 1}: "
                f"{exception} ({action})."
            )
            self.wake.set()

    async def download_loop(self) -> None:
        """Process serially so duplicate plays cannot race before library scans."""

        while True:
            self.queue_wake.clear()
            row = self.db.execute(
                """SELECT plays.* FROM plays JOIN stations ON plays.station=stations.id
                WHERE (plays.status='queued'
                OR (plays.status='retry' AND plays.retry_at<=?)) AND stations.enabled=1
                ORDER BY CASE plays.status WHEN 'queued' THEN 0 ELSE 1 END,
                plays.played_at, plays.id LIMIT 1""",
                (time.time(),),
            ).fetchone()
            if row is None:
                retry_at = self.db.execute("""SELECT MIN(plays.retry_at) FROM plays
                    JOIN stations ON plays.station=stations.id
                    WHERE plays.status='retry' AND stations.enabled=1""").fetchone()[0]
                try:
                    await asyncio.wait_for(
                        self.queue_wake.wait(),
                        timeout=(
                            max(0, retry_at - time.time())
                            if retry_at is not None
                            else None
                        ),
                    )
                except asyncio.TimeoutError:
                    pass
            else:
                await self.process_play(row)

    async def publish_playlists(self, start: datetime, end: datetime) -> None:
        """Resolve a day's monitored plays before replacing any remote playlist."""

        stations = [
            row["id"]
            for row in self.db.execute("SELECT id FROM stations WHERE enabled=1")
        ]
        if not stations:
            return
        connection = self.navidrome()
        tracks: dict[str, list[str]] = {station: [] for station in stations}
        resolved: dict[str, str | None] = {}
        rows = self.db.execute(
            """SELECT plays.* FROM plays JOIN stations ON plays.station=stations.id
            WHERE stations.enabled=1 AND played_at>=? AND played_at<?
            ORDER BY played_at, plays.id""",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        for row in rows:
            play = RadioPlay(row["played_at"], row["artist"], row["title"])
            if play.song_key not in resolved:
                resolved[play.song_key] = await asyncio.to_thread(
                    connection.find_song_id, play.search_artist, play.title
                )
                if resolved[play.song_key] is None:
                    self.append_log(
                        f"Playlists: not in Navidrome yet; skipping {play.artist} - {play.title}."
                    )
            song_id = resolved[play.song_key]
            if song_id is not None:
                tracks[row["station"]].append(song_id)
        current_stations = [
            row["id"]
            for row in self.db.execute("SELECT id FROM stations WHERE enabled=1")
        ]
        if connection != self.navidrome() or stations != current_stations:
            raise RadioError("Radio settings changed; playlist publishing will retry.")
        mix = []
        for station, song_ids in tracks.items():
            mix.extend(song_ids)
            name = f"spotDL Radio - {STATIONS[station]}"
            await asyncio.to_thread(connection.replace_playlist, name, song_ids)
            self.append_log(
                f"Playlist {name}: replaced with {len(set(song_ids))} unique song(s)."
            )
        await asyncio.to_thread(
            connection.replace_playlist, "spotDL Radio - Super Mix", mix
        )
        self.append_log(
            f"Playlist spotDL Radio - Super Mix: replaced with {len(set(mix))} unique song(s)."
        )

    async def check_playlists(self, now: float) -> None:
        """Persist the schedule, catch up after downtime, and retry failed runs."""

        if not self.db.execute(
            "SELECT 1 FROM stations WHERE enabled=1 LIMIT 1"
        ).fetchone():
            return
        start, end, upcoming = playlist_window(
            datetime.fromtimestamp(now, timezone.utc)
        )
        state = self.db.execute("SELECT * FROM playlist_state WHERE id=1").fetchone()
        if not state["next_run"]:
            with self.db:
                self.db.execute(
                    "UPDATE playlist_state SET next_run=? WHERE id=1",
                    (upcoming.timestamp(),),
                )
            return
        if (
            now < state["next_run"]
            or now < state["attempted_at"] + PLAYLIST_RETRY_INTERVAL
        ):
            return
        with self.db:
            self.db.execute(
                "UPDATE playlist_state SET attempted_at=? WHERE id=1", (now,)
            )
        self.append_log(
            f"Publishing radio playlists for {start.isoformat()} to {end.isoformat()}..."
        )
        try:
            await self.publish_playlists(start, end)
        except Exception as exception:  # pylint: disable=broad-exception-caught
            logger.exception("Nightly radio playlist publishing failed")
            self.append_log(f"ERROR Playlists: {exception}; retrying in 5 minutes.")
            with self.db:
                self.db.execute(
                    "UPDATE playlist_state SET error=? WHERE id=1", (str(exception),)
                )
        else:
            with self.db:
                self.db.execute(
                    """UPDATE playlist_state SET next_run=?, last_run=?, error=''
                    WHERE id=1""",
                    (upcoming.timestamp(), end.isoformat()),
                )

    async def playlist_loop(self) -> None:
        """Run the nightly schedule independently of downloads and browser tabs."""

        while True:
            await self.check_playlists(time.time())
            await asyncio.sleep(30)
