import asyncio
import logging
from types import SimpleNamespace

import pytest

from spotdl.providers.audio.base import YTDLLogger
from spotdl.types.options import DownloaderOptions
from spotdl.utils import web as web_utils
from spotdl.utils.config import DOWNLOADER_OPTIONS
from spotdl.utils.navidrome import NavidromeError
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
    monkeypatch.setattr(
        app_state, "radio_monitor", SimpleNamespace(optional_navidrome=lambda: None)
    )


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


def test_workspace_template_places_search_and_downloads_together():
    rendered = routes.templates.get_template("workspace.html.j2").render(
        search_term="grenade", placeholder_item="Search Spotify"
    )

    assert 'id="search-input"' in rendered
    assert 'id="search-list"' in rendered
    assert 'id="download-list"' in rendered
    assert "grid-cols-1" in rendered
    assert "lg:grid-cols-" in rendered


def test_download_list_renders_empty_state():
    rendered = routes.templates.get_template("download-list.html.j2").render(
        client_song_downloads=[]
    )

    assert "No active downloads." in rendered


def test_home_uses_tab_session_without_footer():
    rendered = routes.templates.get_template("home.html.j2").render()

    assert "sessionStorage.getItem('client_id')" in rendered
    assert "sessionStorage.setItem('client_id'" in rendered
    assert "localStorage" not in rendered
    assert "<footer" not in rendered
    assert "Session downloads" in rendered
    assert 'id="download-output"' in rendered


def test_failed_download_is_not_shown_as_complete():
    rendered = routes.templates.get_template("download-list.html.j2").render(
        client_song_downloads=[
            SimpleNamespace(
                song=SimpleNamespace(name="Track", artist="Artist"),
                message="Error",
                progress=100,
            )
        ]
    )

    assert "Failed" in rendered
    assert ">Complete<" not in rendered
    assert "Download file" not in rendered


async def test_downloads_stream_only_current_client_output(client):
    client.append_log("ERROR <script>bad()</script>")
    other_client = Client("other-client")
    other_client.append_log("private output")
    other_client.downloader.progress_handler.progress_tracker.add(
        SimpleNamespace(url="other-track", name="Another session's track")
    )
    response = await routes.handle_get_client_downloads({"client_id": client.client_id})
    stream = response.body_iterator
    try:
        downloads = await anext(stream)
        output = await anext(stream)
    finally:
        await stream.aclose()

    assert "No active downloads." in downloads
    assert "Another session's track" not in downloads
    assert "ERROR &lt;script&gt;bad()&lt;/script&gt;" in output
    assert "private output" not in output


def test_settings_keep_session_downloads(client):
    song = SimpleNamespace(url="session-track", name="Session track")
    tracker = client.downloader.progress_handler.progress_tracker
    tracker.add(song)
    client.update_settings({"format": "flac"})

    assert client.downloader.progress_handler.progress_tracker is tracker
    assert tracker.songs[song.url].song is song


def test_search_limits_results(client, monkeypatch):
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
    assert "Searching Spotify: query" in client.get_log_output()
    assert "Search complete" in client.get_log_output()


def test_search_timeout_replaces_loading_state(client, monkeypatch):
    async def timeout(*args, **kwargs):
        raise asyncio.TimeoutError

    monkeypatch.setattr(routes.asyncio, "to_thread", timeout)

    response = asyncio.run(
        routes.handle_get_client_search(
            {"client_id": "test-client", "search_term": "query"}
        )
    )

    assert "Search timed out after 30 seconds" in read_response(response)
    assert "ERROR Search timed out after 30 seconds" in client.get_log_output()


def test_search_error_is_logged_with_traceback(client, monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError("<provider error>")

    monkeypatch.setattr(routes, "get_search_suggestions", fail)
    response = asyncio.run(
        routes.handle_get_client_search(
            {"client_id": client.client_id, "search_term": "daft punk"}
        )
    )

    assert "Search failed: &lt;provider error&gt;" in read_response(response)
    output = client.get_log_output()
    assert "Searching Spotify: daft punk" in output
    assert "ERROR Search failed: <provider error>" in output
    assert "Traceback (most recent call last)" in output
    assert "ValueError: <provider error>" in output


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
    songs = [
        SimpleNamespace(name="first", artist="Artist", url="first"),
        SimpleNamespace(name="second", artist="Artist", url="second"),
    ]
    parsed_queries = []
    downloaded_songs = []
    url = "https://open.spotify.com/album/example"

    def fake_get_simple_songs(query, **kwargs):
        parsed_queries.append(query)
        return songs

    async def fake_pool_download(song):
        downloaded_songs.append(song)
        await asyncio.to_thread(YTDLLogger().warning, f"warning for {song.name}")
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
    assert "WARNING [yt-dlp] warning for first" in client.get_log_output()
    assert "Download complete." in client.get_log_output()
    assert (
        "Navidrome is not configured; library check skipped" in client.get_log_output()
    )


@pytest.mark.parametrize("collected", [True, False])
def test_regular_download_checks_navidrome_before_audio(
    client, monkeypatch, mocker, tmp_path, collected
):
    monkeypatch.setattr(app_state, "web_settings", {}, raising=False)
    song = SimpleNamespace(name="Track", artist="Artist", url="track")
    connection = mocker.Mock()
    connection.contains.return_value = collected
    monkeypatch.setattr(
        app_state,
        "radio_monitor",
        SimpleNamespace(optional_navidrome=lambda: connection),
    )
    monkeypatch.setattr(routes, "get_simple_songs", lambda *args, **kwargs: [song])

    async def download(selected):
        connection.contains.assert_called_once_with("Artist", "Track")
        return selected, tmp_path / "song.mp3"

    audio = mocker.AsyncMock(side_effect=download)
    monkeypatch.setattr(client.downloader, "pool_download", audio, raising=False)
    response = asyncio.run(
        routes.handle_post_client_download(
            {"client_id": client.client_id, "song_url": "track"}
        )
    )
    output = read_response(response)

    connection.contains.assert_called_once_with("Artist", "Track")
    if collected:
        audio.assert_not_called()
        assert "1 already collected" in output
        rendered = routes.templates.get_template("download-list.html.j2").render(
            client_song_downloads=client.progress_handler.progress_tracker.songs.values()
        )
        assert "Already collected" in rendered
        assert "Download file" not in rendered
        assert ">Complete<" not in rendered
    else:
        audio.assert_awaited_once_with(song)
        assert "Download complete." in output


def test_collection_checks_each_song_and_library_errors_block_only_failed_tracks(
    client, monkeypatch, mocker, tmp_path
):
    monkeypatch.setattr(app_state, "web_settings", {}, raising=False)
    songs = [
        SimpleNamespace(name=title, artist="Artist", url=title)
        for title in ("Collected", "Missing", "Blocked")
    ]
    connection = mocker.Mock()

    def contains(artist, title):
        if title == "Blocked":
            raise NavidromeError("Library unavailable")
        return title == "Collected"

    connection.contains.side_effect = contains
    monkeypatch.setattr(
        app_state,
        "radio_monitor",
        SimpleNamespace(optional_navidrome=lambda: connection),
    )
    monkeypatch.setattr(routes, "get_simple_songs", lambda *args, **kwargs: songs)
    audio = mocker.AsyncMock(return_value=(songs[1], tmp_path / "missing.mp3"))
    monkeypatch.setattr(client.downloader, "pool_download", audio, raising=False)
    response = asyncio.run(
        routes.handle_post_client_download(
            {"client_id": client.client_id, "song_url": "album"}
        )
    )
    output = read_response(response)

    audio.assert_awaited_once_with(songs[1])
    assert connection.contains.call_count == 3
    assert "1 failed track(s)" in output
    assert "1 already collected" in output
    assert "Library unavailable" in client.get_log_output()
    assert client.progress_handler.progress_tracker.songs["Blocked"].message == "Error"


def test_download_preparation_error_is_logged_and_escaped(client, monkeypatch):
    monkeypatch.setattr(app_state, "web_settings", {}, raising=False)

    def fail(*args, **kwargs):
        raise ValueError("<invalid URL>")

    monkeypatch.setattr(routes, "get_simple_songs", fail)
    response = asyncio.run(
        routes.handle_post_client_download(
            {"client_id": client.client_id, "song_url": "invalid"}
        )
    )
    response_text = read_response(response)

    assert "&lt;invalid URL&gt;" in response_text
    assert (
        "ERROR Could not process Spotify URL: <invalid URL>" in client.get_log_output()
    )


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
