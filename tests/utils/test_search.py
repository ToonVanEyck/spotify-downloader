import pytest

from spotdl.types.saved import SavedError
from spotdl.types.song import Song, SongListError
from spotdl.utils import search as search_utils
from spotdl.utils.search import (
    get_search_results,
    get_search_suggestions,
    get_simple_songs,
    get_spotify_url_preview,
    parse_query,
)

SONG = ["https://open.spotify.com/track/2Ikdgh3J5vCRmnCL3Xcrtv"]
PLAYLIST = ["https://open.spotify.com/playlist/78Lg6HmUqlTnmipvNxc536"]
ALBUM = ["https://open.spotify.com/album/4MQnUDGXmHOvnsWCpzeqWT"]
YT = [
    "https://www.youtube.com/watch?v=BZKwsPIhVO8|https://open.spotify.com/track/4B2kkxg3wKSTZw5JPaUtzQ"
]
ARTIST = ["https://open.spotify.com/artist/1FPC2zwfMHhrP3frOfaai6"]
ALBUM_SEARCH = ["album: yeezus"]

QUERY = SONG + PLAYLIST + ALBUM + YT + ARTIST

SAVED = ["saved"]


@pytest.mark.vcr()
def test_parse_song():
    songs = parse_query(SONG)

    song = songs[0]
    assert len(songs) == 1
    assert song.download_url == None


@pytest.mark.vcr()
def test_parse_album():
    songs = parse_query(ALBUM)

    assert len(songs) > 1
    assert songs[0].url == "https://open.spotify.com/track/2Ikdgh3J5vCRmnCL3Xcrtv"


@pytest.mark.vcr()
def test_parse_yt_link():
    songs = parse_query(YT)

    assert len(songs) == 1
    assert songs[0].url == "https://open.spotify.com/track/4B2kkxg3wKSTZw5JPaUtzQ"
    assert songs[0].download_url == "https://www.youtube.com/watch?v=BZKwsPIhVO8"


@pytest.mark.vcr()
def test_parse_artist():
    songs = parse_query(ARTIST)

    assert len(songs) > 1


@pytest.mark.xfail(
    raises=SongListError,
    strict=True,
    reason="SpotipyFree's search() ignores type= and only returns tracks",
)
@pytest.mark.vcr()
def test_parse_album_search():
    songs = parse_query(ALBUM_SEARCH)

    assert len(songs) > 0


@pytest.mark.vcr()
def test_parse_saved():
    with pytest.raises(SavedError):
        parse_query(SAVED)


@pytest.mark.vcr()
def test_parse_query():
    songs = parse_query(QUERY)

    assert len(songs) > 1


@pytest.mark.vcr()
def test_get_search_results():
    results = get_search_results("test")
    assert len(results) > 1


def test_get_search_suggestions_returns_all_categories(monkeypatch):
    query_calls = []

    class FakeSession:
        auto_retries = 4

        def request(self, method, url, **kwargs):
            pytest.fail("The fake search should not make an HTTP request")

    session = FakeSession()

    class FakeSearch:
        def __init__(self, client):
            assert client is session

        def query_songs(self, query, limit):
            query_calls.append((query, limit))
            return {
                "data": {
                    "searchV2": {
                        "artists": {
                            "items": [
                                {
                                    "data": {
                                        "profile": {"name": "Artist"},
                                        "uri": "spotify:artist:artist-id",
                                        "visuals": {
                                            "avatarImage": {
                                                "sources": [
                                                    {
                                                        "url": "https://example.com/artist.jpg",
                                                        "width": 100,
                                                        "height": 100,
                                                    }
                                                ]
                                            }
                                        },
                                    }
                                },
                                {
                                    "data": {
                                        "profile": {"name": "Mars Argo"},
                                        "uri": "spotify:artist:unrelated-id",
                                    }
                                },
                            ]
                        },
                        "tracksV2": {
                            "items": [
                                {
                                    "item": {
                                        "data": {
                                            "name": "Track",
                                            "uri": "spotify:track:track-id",
                                            "artists": {
                                                "items": [
                                                    {"profile": {"name": "Artist"}}
                                                ]
                                            },
                                            "albumOfTrack": {
                                                "coverArt": {
                                                    "sources": [
                                                        {
                                                            "url": "https://example.com/track.jpg",
                                                            "width": 100,
                                                            "height": 100,
                                                        }
                                                    ]
                                                }
                                            },
                                        }
                                    }
                                }
                            ]
                        },
                        "albumsV2": {
                            "items": [
                                {
                                    "data": {
                                        "name": "Album",
                                        "uri": "spotify:album:album-id",
                                        "artists": {
                                            "items": [{"profile": {"name": "Artist"}}]
                                        },
                                        "coverArt": {
                                            "sources": [
                                                {
                                                    "url": "https://example.com/album.jpg",
                                                    "width": 100,
                                                    "height": 100,
                                                }
                                            ]
                                        },
                                    }
                                }
                            ]
                        },
                        "playlists": {
                            "items": [
                                {
                                    "data": {
                                        "name": "Artist Mix",
                                        "uri": "spotify:playlist:playlist-id",
                                        "ownerV2": {"data": {"name": "Playlist Owner"}},
                                        "images": {
                                            "items": [
                                                {
                                                    "sources": [
                                                        {
                                                            "url": "https://example.com/playlist.jpg",
                                                            "width": 100,
                                                            "height": 100,
                                                        }
                                                    ]
                                                }
                                            ]
                                        },
                                    }
                                }
                            ]
                        },
                    }
                }
            }

    monkeypatch.setattr(search_utils, "SpotifySearchSong", FakeSearch)
    monkeypatch.setattr(search_utils.client_pool, "get", lambda: session)
    monkeypatch.setattr(search_utils.client_pool, "put", lambda client: None)

    suggestions = get_search_suggestions("Artist", limit=3)

    assert query_calls == [("Artist", 9)]
    assert [items[0]["name"] for items in suggestions.values()] == [
        "Artist",
        "Track",
        "Album",
        "Artist Mix",
    ]
    assert suggestions["tracks"][0]["url"] == (
        "https://open.spotify.com/track/track-id"
    )
    assert [artist["name"] for artist in suggestions["artists"]] == ["Artist"]
    assert suggestions["playlists"][0]["url"] == (
        "https://open.spotify.com/playlist/playlist-id"
    )


@pytest.mark.parametrize("kind", ["track", "album", "playlist", "artist"])
def test_spotify_url_preview(monkeypatch, kind):
    class FakeSpotifyClient:
        def album(self, url):
            return {
                "name": "Album",
                "artists": [{"name": "Artist"}],
                "images": [],
            }

        def playlist(self, url):
            return {
                "name": "Playlist",
                "owner": {"display_name": "Owner"},
                "tracks": {"total": 8},
                "images": [],
            }

        def artist(self, url):
            return {"name": "Artist", "images": []}

    monkeypatch.setattr(search_utils, "SpotifyClient", FakeSpotifyClient)
    monkeypatch.setattr(
        search_utils.Song,
        "from_url",
        lambda url: type(
            "FakeSong",
            (),
            {"name": "Track", "artists": ["Artist"], "cover_url": None},
        )(),
    )

    preview = get_spotify_url_preview(f"https://open.spotify.com/{kind}/item-id")

    assert preview["kind"] == kind.title()
    assert preview["url"] == f"https://open.spotify.com/{kind}/item-id"


def test_create_empty_song():
    song = Song.from_missing_data(name="test")
    assert song.name == "test"
    assert song.url == None
    assert song.download_url == None
    assert song.duration == None
    assert song.artists == None


@pytest.mark.vcr()
def test_get_simple_songs():
    songs = get_simple_songs(QUERY)
    assert len(songs) > 1


def test_get_simple_songs_skips_missing_video_details(monkeypatch):
    class FakeYTMClient:
        def get_song(self, video_id):
            return {"playabilityStatus": {"status": "ERROR"}}

    monkeypatch.setattr("spotdl.utils.search.get_ytm_client", lambda: FakeYTMClient())

    songs = get_simple_songs(["https://music.youtube.com/watch?v=dQw4w9WgXcQ"])

    assert songs == []
