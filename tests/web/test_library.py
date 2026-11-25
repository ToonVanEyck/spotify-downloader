import asyncio
import logging
import stat
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI, Request

from spotdl.types.options import DownloaderOptions
from spotdl.utils.config import DOWNLOADER_OPTIONS
from spotdl.utils.navidrome import Navidrome
from spotdl.utils.radio import RadioMonitor
from spotdl.utils.web import Client, DownloadQueue, WebDownloadResult, app_state
from spotdl.web import library
from tests.web.test_datastar import call_asgi


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    instance = RadioMonitor(
        tmp_path / "radio.sqlite3", DownloaderOptions(**DOWNLOADER_OPTIONS)
    )
    monkeypatch.setattr(app_state, "radio_monitor", instance)
    monkeypatch.setattr(app_state, "download_queue", DownloadQueue())
    monkeypatch.setattr(
        app_state, "logger", logging.getLogger("library-test"), raising=False
    )
    monkeypatch.setattr(
        app_state, "downloader_settings", DOWNLOADER_OPTIONS.copy(), raising=False
    )
    yield instance
    instance.db.close()


async def test_regular_radio_and_repair_downloads_share_a_fair_queue(monitor, mocker):
    client = Client.__new__(Client)
    client.progress_handler = mocker.Mock()
    started = asyncio.Event()
    release = asyncio.Event()
    order = []

    async def regular(song):
        order.append(song.name)
        if song.name == "first":
            started.set()
            await release.wait()
        return WebDownloadResult(song)

    async def radio_download(row):
        order.append("radio")

    async def repair(*args):
        order.append("repair")

    mocker.patch.object(client, "_download_song", side_effect=regular)
    mocker.patch.object(monitor, "_process_play", side_effect=radio_download)
    mocker.patch.object(library, "_replace_file_now", side_effect=repair)
    navidrome = mocker.Mock(spec=Navidrome)

    first = asyncio.create_task(client.download_song(SimpleNamespace(name="first")))
    await started.wait()
    radio_task = asyncio.create_task(monitor.process_play({}))
    await asyncio.sleep(0)
    repair_task = asyncio.create_task(
        library._replace_file(navidrome, "song-123", "source")
    )
    await asyncio.sleep(0)
    second = asyncio.create_task(client.download_song(SimpleNamespace(name="second")))
    await asyncio.sleep(0)
    assert order == ["first"]

    release.set()
    await asyncio.wait_for(
        asyncio.gather(first, radio_task, repair_task, second), timeout=1
    )
    assert order == ["first", "radio", "repair", "second"]


def make_request(path="/library"):
    return Request(
        {
            "type": "http",
            "scheme": "http",
            "method": "GET",
            "path": path,
            "query_string": b"",
            "headers": [(b"host", b"localhost:8800")],
            "server": ("localhost", 8800),
        }
    )


def test_youtube_video_id_accepts_only_supported_secure_urls():
    assert (
        library._youtube_video_id("https://music.youtube.com/watch?v=abcdefghijk")
        == "abcdefghijk"
    )
    assert library._youtube_video_id("https://youtu.be/abcdefghijk") == "abcdefghijk"
    assert library._youtube_video_id("http://youtube.com/watch?v=abcdefghijk") is None
    assert (
        library._youtube_video_id(
            "https://youtube.com.evil.example/watch?v=abcdefghijk"
        )
        is None
    )


def test_library_file_stays_inside_configured_music_directory(tmp_path, monkeypatch):
    root = tmp_path / "music"
    root.mkdir()
    song = root / "song.mp3"
    song.write_bytes(b"audio")
    (tmp_path / "song.mp3").write_bytes(b"outside")
    monkeypatch.setenv("NAVIDROME_MUSIC_DIR", str(root))

    assert library._library_file("song.mp3") == song
    with pytest.raises(ValueError, match="outside the music directory"):
        library._library_file("../song.mp3")
    with pytest.raises(ValueError, match="outside the music directory"):
        library._library_file(str(tmp_path / "song.mp3"))


def test_library_root_reuses_relative_web_download_location(tmp_path, monkeypatch):
    song = tmp_path / "Artist" / "song.mp3"
    song.parent.mkdir()
    song.write_bytes(b"audio")
    monkeypatch.delenv("NAVIDROME_MUSIC_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        app_state, "downloader_settings", DOWNLOADER_OPTIONS.copy(), raising=False
    )
    monkeypatch.setattr(
        app_state, "web_settings", {"web_use_output_dir": True}, raising=False
    )

    assert library._library_file("Artist/song.mp3") == song


def test_library_root_reuses_absolute_web_output_template(tmp_path, monkeypatch):
    song = tmp_path / "Artist" / "song.mp3"
    song.parent.mkdir()
    song.write_bytes(b"audio")
    settings = DOWNLOADER_OPTIONS.copy()
    settings["output"] = str(tmp_path / "{artist}" / "{title}.{output-ext}")
    monkeypatch.delenv("NAVIDROME_MUSIC_DIR", raising=False)
    monkeypatch.setattr(app_state, "downloader_settings", settings, raising=False)
    monkeypatch.setattr(
        app_state, "web_settings", {"web_use_output_dir": True}, raising=False
    )

    assert library._library_file("Artist/song.mp3") == song


def test_source_suggestions_exclude_current_and_deduplicate(monkeypatch):
    from spotdl.types.result import Result

    def result(video_id, source):
        return Result(
            source=source,
            url=f"https://www.youtube.com/watch?v={video_id}",
            verified=True,
            name=video_id,
            duration=100,
            author="Artist",
            result_id=video_id,
        )

    class FakeProvider:
        GET_RESULTS_OPTS = [{}]

        def __init__(self, results):
            self.results = results

        def get_results(self, _query, **_options):
            return self.results

    monkeypatch.setattr(
        library,
        "YouTubeMusic",
        lambda **_kwargs: FakeProvider(
            [result("abcdefghijk", "ytmusic"), result("lmnopqrstuv", "ytmusic")]
        ),
    )
    monkeypatch.setattr(
        library,
        "YouTube",
        lambda **_kwargs: FakeProvider(
            [result("lmnopqrstuv", "youtube"), result("ABCDEFGHIJK", "youtube")]
        ),
    )
    monkeypatch.setattr(
        app_state, "downloader_settings", DOWNLOADER_OPTIONS.copy(), raising=False
    )

    results = library._source_candidates(
        "Artist", "Song", "https://music.youtube.com/watch?v=abcdefghijk"
    )

    assert [item.result_id for item in results] == ["lmnopqrstuv", "ABCDEFGHIJK"]


async def test_replace_file_downloads_to_neighbor_then_overwrites(
    tmp_path, monkeypatch, mocker
):
    root = tmp_path / "music"
    root.mkdir()
    target = root / "song.mp3"
    target.write_bytes(b"bad recording")
    target.chmod(0o640)
    monkeypatch.setenv("NAVIDROME_MUSIC_DIR", str(root))
    monkeypatch.setattr(
        app_state, "downloader_settings", DOWNLOADER_OPTIONS.copy(), raising=False
    )
    navidrome = mocker.Mock(spec=Navidrome)
    navidrome.get_song.return_value = {
        "id": "song-123",
        "title": "Song",
        "artist": "Artist",
        "album": "Album",
        "path": "song.mp3",
    }
    mocker.patch.object(
        library,
        "get_file_metadata",
        return_value={
            "name": "Song",
            "artist": "Artist",
            "artists": ["Artist"],
            "download_url": "https://music.youtube.com/watch?v=abcdefghijk",
        },
    )
    downloaded_songs = []

    class FakeDownloader:
        def __init__(self, settings, loop):
            self.settings = settings

        async def pool_download(self, song):
            downloaded_songs.append(song)
            output = Path(
                self.settings["output"].replace("{output-ext}", self.settings["format"])
            )
            output.write_bytes(b"better recording")
            return song, output

    mocker.patch.object(library, "Downloader", FakeDownloader)

    await library._replace_file(
        navidrome, "song-123", "https://www.youtube.com/watch?v=lmnopqrstuv"
    )

    assert target.read_bytes() == b"better recording"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert downloaded_songs[0].download_url == (
        "https://www.youtube.com/watch?v=lmnopqrstuv"
    )
    assert not list(root.glob(".spotdl-replacement-*"))


async def test_failed_replacement_preserves_original(tmp_path, monkeypatch, mocker):
    root = tmp_path / "music"
    root.mkdir()
    target = root / "song.mp3"
    target.write_bytes(b"original recording")
    monkeypatch.setenv("NAVIDROME_MUSIC_DIR", str(root))
    monkeypatch.setattr(
        app_state, "downloader_settings", DOWNLOADER_OPTIONS.copy(), raising=False
    )
    navidrome = mocker.Mock(spec=Navidrome)
    navidrome.get_song.return_value = {
        "id": "song-123",
        "title": "Song",
        "artist": "Artist",
        "path": "song.mp3",
    }
    mocker.patch.object(
        library,
        "get_file_metadata",
        return_value={
            "name": "Song",
            "artist": "Artist",
            "artists": ["Artist"],
            "download_url": "https://music.youtube.com/watch?v=abcdefghijk",
        },
    )

    class FailedDownloader:
        def __init__(self, settings, loop):
            pass

        async def pool_download(self, song):
            return song, None

    mocker.patch.object(library, "Downloader", FailedDownloader)

    with pytest.raises(RuntimeError, match="did not produce"):
        await library._replace_file(
            navidrome, "song-123", "https://www.youtube.com/watch?v=lmnopqrstuv"
        )

    assert target.read_bytes() == b"original recording"
    assert not list(root.glob(".spotdl-replacement-*"))


async def test_library_page_searches_and_renders_navidrome_results(
    monitor, tmp_path, monkeypatch, mocker
):
    monkeypatch.setenv("NAVIDROME_MUSIC_DIR", str(tmp_path))
    target = tmp_path / "song.mp3"
    target.write_bytes(b"audio")
    navidrome = mocker.Mock(spec=Navidrome)
    navidrome.search_songs.return_value = [
        {
            "id": "song-123",
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
        }
    ]
    navidrome.get_song.return_value = {
        "id": "song-123",
        "title": "Song",
        "artist": "Artist",
        "album": "Album",
        "path": "song.mp3",
    }
    mocker.patch.object(monitor, "navidrome", return_value=navidrome)
    mocker.patch.object(library, "get_file_metadata", return_value={"download_url": ""})
    mocker.patch.object(library, "_source_candidates", return_value=[])

    response = await library.library_page(
        make_request(), q="Artist Song", song_id="song-123"
    )
    html = response.body.decode()

    assert "Song" in html
    assert "Find replacement sources" in html
    assert "No alternative YouTube sources were found." in html
    assert "song.mp3" not in html
    assert 'href="/" class="tab tab-active"' not in html
    assert 'href="/library" class="tab tab-active"' in html
    navidrome.search_songs.assert_called_once_with("Artist Song")
    navidrome.get_song.assert_called_once_with("song-123")


async def test_replace_route_accepts_form_and_checks_origin(monitor, mocker):
    navidrome = mocker.Mock(spec=Navidrome)
    mocker.patch.object(monitor, "navidrome", return_value=navidrome)
    replace_file = mocker.patch.object(library, "_replace_file")
    app = FastAPI()
    app.include_router(library.router)
    body = urlencode(
        {
            "song_id": "song-123",
            "source_url": "https://www.youtube.com/watch?v=abcdefghijk",
        }
    ).encode()

    status, headers, _ = await call_asgi(
        app,
        "/library/replace",
        method="POST",
        request_body=body,
        request_headers=(
            (b"host", b"localhost:8800"),
            (b"origin", b"http://localhost:8800"),
            (b"content-type", b"application/x-www-form-urlencoded"),
        ),
    )

    assert status == 303
    assert headers[b"location"] == b"/library?replaced=true"
    replace_file.assert_awaited_once_with(
        navidrome, "song-123", "https://www.youtube.com/watch?v=abcdefghijk"
    )
