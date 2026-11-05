import asyncio
import logging
from types import SimpleNamespace

import pytest

from spotdl.types.options import DownloaderOptions
from spotdl.utils import web as web_utils
from spotdl.utils.config import DOWNLOADER_OPTIONS
from spotdl.utils.web import Client, app_state
from spotdl.web import routes


@pytest.fixture(autouse=True)
def fake_app_state(monkeypatch):
    """Set up the parts of the app state that clients use"""

    monkeypatch.setattr(
        app_state,
        "downloader_settings",
        DownloaderOptions(**DOWNLOADER_OPTIONS),  # type: ignore
        raising=False,
    )
    monkeypatch.setattr(app_state, "loop", None, raising=False)
    monkeypatch.setattr(
        app_state, "logger", logging.getLogger("spotdl-test"), raising=False
    )
    monkeypatch.setattr(app_state, "clients", {})


@pytest.fixture(autouse=True)
def fake_downloader(monkeypatch):
    """Don't create a real downloader, it needs ffmpeg"""

    monkeypatch.setattr(
        web_utils,
        "Downloader",
        lambda settings, loop=None: SimpleNamespace(settings=dict(settings)),
    )


@pytest.fixture(autouse=True)
def no_status_delay(monkeypatch):
    """Don't wait before clearing the settings status message"""

    real_sleep = asyncio.sleep

    async def fake_sleep(delay, *args, **kwargs):
        await real_sleep(0)

    monkeypatch.setattr(routes.asyncio, "sleep", fake_sleep)


@pytest.fixture()
def client():
    client = Client("test-client")
    app_state.clients[client.client_id] = client

    return client


def post_settings(settings):
    """Call the settings handler and return the events it sends as text"""

    signals = {"client_id": "test-client"}
    if settings is not None:
        signals["downloader_settings"] = settings

    async def run():
        response = await routes.handle_post_client_settings(signals)
        return "".join(
            [
                chunk if isinstance(chunk, str) else chunk.decode()
                async for chunk in response.body_iterator
            ]
        )

    return asyncio.run(run())


def read_response(response):
    async def run():
        return "".join(
            [
                chunk if isinstance(chunk, str) else chunk.decode()
                async for chunk in response.body_iterator
            ]
        )

    return asyncio.run(run())


def test_search_limits_results(monkeypatch):
    search_calls = []

    def fake_search(search_term, limit=None):
        search_calls.append((search_term, limit))
        return {
            "artists": [
                {
                    "kind": "Artist",
                    "name": "Bruno Mars",
                    "url": "https://open.spotify.com/artist/id",
                }
            ],
            "tracks": [
                {
                    "kind": "Track",
                    "name": "Grenade",
                    "url": "https://open.spotify.com/track/id",
                }
            ],
            "albums": [
                {
                    "kind": "Album",
                    "name": "Doo-Wops & Hooligans",
                    "url": "https://open.spotify.com/album/id",
                }
            ],
            "playlists": [
                {
                    "kind": "Playlist",
                    "name": "Grenade Mix",
                    "url": "https://open.spotify.com/playlist/id",
                }
            ],
        }

    monkeypatch.setattr(routes, "get_search_suggestions", fake_search)

    response = asyncio.run(
        routes.handle_get_client_search(
            {"client_id": "test-client", "search_term": "query"}
        )
    )
    response_text = read_response(response)

    assert search_calls == [("query", 10)]
    assert all(
        title in response_text for title in ["Artists", "Tracks", "Albums", "Playlists"]
    )
    assert "Grenade" in response_text
    assert "&#34;https://open.spotify.com/track/id&#34;" in response_text


def test_search_timeout_replaces_loading_state(monkeypatch):
    async def timeout(*args, **kwargs):
        raise asyncio.TimeoutError

    monkeypatch.setattr(routes.asyncio, "to_thread", timeout)

    response = asyncio.run(
        routes.handle_get_client_search(
            {"client_id": "test-client", "search_term": "query"}
        )
    )

    assert "Search timed out after 30 seconds" in read_response(response)


@pytest.mark.parametrize("collection", ["track", "album", "playlist", "artist"])
def test_search_url_shows_preview_without_downloading(monkeypatch, collection):
    url = f"https://open.spotify.com/{collection}/example"
    preview = {
        "kind": collection.title(),
        "name": f"Example {collection}",
        "subtitle": "Preview metadata",
        "cover_url": None,
        "url": url,
    }

    monkeypatch.setattr(routes, "get_spotify_url_preview", lambda _: preview)
    monkeypatch.setattr(
        routes,
        "get_simple_songs",
        lambda *args, **kwargs: pytest.fail("Preview should not start a download"),
    )

    response = asyncio.run(
        routes.handle_get_client_search(
            {
                "client_id": "test-client",
                "search_term": url,
            }
        )
    )
    response_text = read_response(response)

    assert preview["name"] in response_text
    assert "Spotify link" in response_text
    assert "Download" in response_text
    assert "Preparing download" not in response_text


def test_download_button_starts_collection_download(client, monkeypatch):
    monkeypatch.setattr(app_state, "web_settings", {}, raising=False)
    songs = [SimpleNamespace(name="first"), SimpleNamespace(name="second")]
    parsed_queries = []
    downloaded_songs = []
    url = "https://open.spotify.com/album/example"

    def fake_get_simple_songs(query, **kwargs):
        parsed_queries.append(query)
        return songs

    async def fake_pool_download(song):
        downloaded_songs.append(song)
        return song, "downloaded"

    monkeypatch.setattr(routes, "get_simple_songs", fake_get_simple_songs)
    monkeypatch.setattr(
        client.downloader, "pool_download", fake_pool_download, raising=False
    )

    response = asyncio.run(
        routes.handle_post_client_download(
            {"client_id": "test-client", "song_url": url}
        )
    )
    response_text = read_response(response)

    assert parsed_queries == [[url]]
    assert downloaded_songs == songs
    assert "Download complete" in response_text


@pytest.mark.parametrize(
    "key, value",
    [
        ("ffmpeg", "calc.exe"),
        ("yt_dlp_args", "--exec 'touch /tmp/pwned'"),
        ("ffmpeg_args", "-y /tmp/pwned"),
        ("output", "/{title}.{output-ext}"),
        ("cookie_file", "/etc/passwd"),
    ],
)
def test_post_settings_ignores_restricted(client, key, value):
    original = client.downloader_settings[key]
    settings = dict(client.downloader_settings)
    settings[key] = value

    response = post_settings(settings)

    assert "settings-is-saved" in response
    assert client.downloader_settings[key] == original
    assert client.downloader.settings[key] == original


def test_post_settings_applies_allowed(client):
    settings = dict(client.downloader_settings)
    settings["format"] = "flac"

    response = post_settings(settings)

    assert "settings-is-saved" in response
    assert client.downloader_settings["format"] == "flac"
    # The downloader is re-initialized with the new settings
    assert client.downloader.settings["format"] == "flac"


@pytest.mark.parametrize("settings", [{"format": "exe"}, "not settings"])
def test_post_settings_rejects_invalid(client, settings):
    original = dict(client.downloader_settings)

    response = post_settings(settings)

    assert "settings-is-not-saved" in response
    assert client.downloader_settings == original


def test_post_settings_without_settings_keeps_settings(client):
    original = dict(client.downloader_settings)

    response = post_settings(None)

    assert "settings-is-saved" in response
    assert client.downloader_settings == original
