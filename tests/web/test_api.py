import asyncio
import logging
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from spotdl.types.options import DownloaderOptions
from spotdl.utils import web as web_utils
from spotdl.utils.arguments import create_parser
from spotdl.utils.config import DOWNLOADER_OPTIONS
from spotdl.utils.navidrome import NavidromeError
from spotdl.utils.web import WEB_EDITABLE_SETTINGS, Client, app_state
from spotdl.web import api


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


@pytest.fixture(autouse=True)
def fake_downloader(monkeypatch):
    """Don't create a real downloader, it needs ffmpeg"""

    monkeypatch.setattr(
        web_utils,
        "Downloader",
        lambda settings, loop=None: SimpleNamespace(settings=dict(settings)),
    )


@pytest.fixture()
def client():
    return Client("test-client")


@pytest.fixture()
def state():
    return SimpleNamespace(
        logger=logging.getLogger("spotdl-test"),
        loop=None,
        web_settings={"web_use_output_dir": False},
        downloader_settings=DownloaderOptions(**DOWNLOADER_OPTIONS),  # type: ignore
    )


def test_editable_settings_are_valid_options():
    dests = {action.dest for action in create_parser()._actions}

    assert WEB_EDITABLE_SETTINGS <= dests
    assert WEB_EDITABLE_SETTINGS <= set(DOWNLOADER_OPTIONS)


@pytest.mark.parametrize(
    "key, value",
    [
        ("ffmpeg", "calc.exe"),
        ("yt_dlp_args", "--exec 'touch /tmp/pwned'"),
        ("ffmpeg_args", "-y /tmp/pwned"),
        ("output", "/etc/{title}.{output-ext}"),
        ("cookie_file", "/etc/passwd"),
        ("archive", "/tmp/archive.txt"),
        ("save_file", "/tmp/save.spotdl"),
        ("save_errors", "/tmp/errors.txt"),
        ("m3u", "/tmp/playlist.m3u"),
        ("proxy", "http://attacker:8080"),
        ("log_level", "DEBUG"),
    ],
)
def test_update_settings_ignores_restricted(client, key, value):
    original = client.downloader_settings[key]

    new_settings = api.update_settings({key: value}, client)  # type: ignore

    assert new_settings[key] == original
    assert client.downloader_settings[key] == original


def test_update_settings_applies_allowed(client):
    new_settings = api.update_settings(
        {"format": "flac", "threads": 2, "audio_providers": ["youtube"]},  # type: ignore
        client,
    )

    assert new_settings["format"] == "flac"
    assert new_settings["threads"] == 2
    assert new_settings["audio_providers"] == ["youtube"]
    # The downloader is re-initialized with the new settings
    assert client.downloader.settings["format"] == "flac"


def test_update_settings_accepts_full_settings_round_trip(client):
    """The web UI posts back every setting it got from /api/settings"""

    settings = dict(client.downloader_settings)
    settings["format"] = "opus"

    new_settings = api.update_settings(settings, client)  # type: ignore

    assert new_settings["format"] == "opus"


@pytest.mark.parametrize(
    "key, value",
    [
        ("format", "exe"),
        ("bitrate", "999k"),
        ("audio_providers", ["youtube", "evil"]),
        ("lyrics_providers", "genius"),
        ("threads", 0),
        ("threads", 10000),
        ("max_filename_length", -1),
    ],
)
def test_update_settings_rejects_invalid_values(client, key, value):
    with pytest.raises(HTTPException) as exc_info:
        api.update_settings({key: value}, client)  # type: ignore

    assert exc_info.value.status_code == 400


def test_get_options_only_returns_editable():
    options = api.get_options()

    assert set(options) == WEB_EDITABLE_SETTINGS


@pytest.mark.parametrize("collected", [True, False])
async def test_download_api_checks_navidrome_and_uses_shared_destination(
    client, state, monkeypatch, mocker, tmp_path, collected
):
    monkeypatch.setattr(web_utils, "get_spotdl_path", lambda: tmp_path)
    song = SimpleNamespace(name="Track", artist="Artist", url="track")
    monkeypatch.setattr(api.Song, "from_url", lambda url: song)
    connection = mocker.Mock()
    connection.contains.return_value = collected
    monkeypatch.setattr(
        app_state,
        "radio_monitor",
        SimpleNamespace(optional_navidrome=lambda: connection),
    )
    path = tmp_path / "web" / "downloads" / "track.mp3"
    audio = mocker.AsyncMock(return_value=(song, path))
    monkeypatch.setattr(client.downloader, "pool_download", audio, raising=False)

    response = await api.download_url("track", client, state)

    connection.contains.assert_called_once_with("Artist", "Track")
    assert client.downloader.settings["output"] == str(path.parent)
    if collected:
        assert response == {"status": "already collected", "song": "Track"}
        audio.assert_not_called()
    else:
        assert response == str(path)
        audio.assert_awaited_once_with(song)


async def test_download_api_blocks_audio_when_navidrome_fails(
    client, state, monkeypatch, mocker
):
    song = SimpleNamespace(name="Track", artist="Artist", url="track")
    monkeypatch.setattr(api.Song, "from_url", lambda url: song)
    connection = mocker.Mock()
    connection.contains.side_effect = NavidromeError("Library unavailable")
    monkeypatch.setattr(
        app_state,
        "radio_monitor",
        SimpleNamespace(optional_navidrome=lambda: connection),
    )
    audio = mocker.AsyncMock()
    monkeypatch.setattr(client.downloader, "pool_download", audio, raising=False)

    with pytest.raises(HTTPException, match="Library unavailable"):
        await api.download_url("track", client, state)
    audio.assert_not_called()
    assert "Library unavailable" in client.get_log_output()


@pytest.mark.parametrize("client_id", ["../../etc", "a/b", "a\\b", "", "x" * 65])
def test_connect_rejects_invalid_client_id(client_id):
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(api.connect_endpoint(client_id))

    assert exc_info.value.status_code == 400


@pytest.fixture()
def download_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "get_spotdl_path", lambda: tmp_path)
    monkeypatch.setattr(web_utils, "get_spotdl_path", lambda: tmp_path)

    path = tmp_path / "web" / "downloads"
    path.mkdir(parents=True)

    return path


def test_download_file_allows_shared_file(client, state, download_dir):
    song = download_dir / "Artist - Title.mp3"
    song.write_bytes(b"")

    response = asyncio.run(api.download_file(str(song), client, state))

    assert response.path == song.resolve()


@pytest.mark.parametrize(
    "relative_path",
    [
        # Path traversal out of the shared download directory
        "../../../../secret.mp3",
        # Old session files are not inside the shared download directory
        "../sessions/other-client/Artist - Title.mp3",
        # Not an audio file
        "Artist - Title.txt",
        "notes.mp3.txt",
    ],
)
def test_download_file_rejects_invalid_paths(
    client, state, download_dir, relative_path
):
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(api.download_file(str(download_dir / relative_path), client, state))

    assert exc_info.value.status_code == 400


def test_download_file_with_output_dir(client, state, tmp_path):
    state.web_settings["web_use_output_dir"] = True
    music_dir = tmp_path / "music"
    state.downloader_settings["output"] = str(music_dir / "{artists} - {title}")

    song = music_dir / "Artist - Title.mp3"
    response = asyncio.run(api.download_file(str(song), client, state))
    assert response.path == song.resolve()

    with pytest.raises(HTTPException):
        asyncio.run(api.download_file(str(tmp_path / "outside.mp3"), client, state))


def test_download_file_with_output_dir_ignores_client_output(client, state, tmp_path):
    """The allowed directory comes from the server's settings, not the client's"""

    state.web_settings["web_use_output_dir"] = True
    state.downloader_settings["output"] = str(tmp_path / "music" / "{title}")
    client.downloader_settings["output"] = str(tmp_path.anchor)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(api.download_file(str(tmp_path / "outside.mp3"), client, state))

    assert exc_info.value.status_code == 400
