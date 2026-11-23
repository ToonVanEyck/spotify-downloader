import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from spotdl.types.options import DownloaderOptions
from spotdl.utils import radio
from spotdl.utils import web as web_utils
from spotdl.utils.config import DOWNLOADER_OPTIONS
from spotdl.utils.navidrome import Navidrome, NavidromeError
from spotdl.utils.radio import (
    RadioError,
    RadioMonitor,
    RadioPlay,
    parse_nostalgie_history,
    parse_radio_history,
    parse_willy_history,
    playlist_window,
)


@pytest.fixture
def monitor(tmp_path):
    instance = RadioMonitor(
        tmp_path / "radio.sqlite3", DownloaderOptions(**DOWNLOADER_OPTIONS)
    )
    yield instance
    instance.db.close()


def enable(monitor, *stations):
    with monitor.db:
        monitor.db.executemany(
            "UPDATE stations SET enabled=1 WHERE id=?", [(value,) for value in stations]
        )


def play(title="Song", minute=0):
    return RadioPlay(f"2026-11-23T12:{minute:02d}:00+00:00", "Artist", title)


def queue_play(monitor, source="stubru", entry=None):
    entry = entry or play()
    with monitor.db:
        monitor.db.execute(
            """INSERT INTO plays
            (station, played_at, artist, title, spotify_url, song_key, status)
            VALUES (?, ?, ?, ?, ?, ?, 'queued')""",
            (
                source,
                entry.played_at,
                entry.artist,
                entry.title,
                entry.spotify_url,
                entry.song_key,
            ),
        )
    return monitor.db.execute("SELECT * FROM plays ORDER BY id DESC LIMIT 1").fetchone()


def test_parse_history_skips_news_and_preserves_play_identity():
    html = """
    <div class="tracks">
      <div class="track">
        <div data-playtime="2026-11-23T12:05:00+01:00"></div>
        <span class="artist">Artist &amp; Guest</span><span class="title">Song</span>
        <div data-spotify-uri="spotify:track:0123456789012345678901"></div>
      </div>
      <div class="track"><span class="title">Nieuws</span></div>
      <div class="track">
        <div data-playtime="2026-11-23T12:00:00+01:00"></div>
        <span class="artist">Artist &amp; Guest</span><span class="title">Song</span>
      </div>
    </div>
    """
    entries = parse_radio_history(html)
    assert len(entries) == 2
    assert entries[0].artist == "Artist & Guest"
    assert entries[0].played_at == "2026-11-23T11:05:00+00:00"
    assert entries[0].spotify_url.endswith("/0123456789012345678901")
    assert not entries[1].spotify_url
    assert entries[0].song_key == entries[1].song_key
    assert entries[0].played_at != entries[1].played_at


def test_changed_source_markup_is_an_error():
    with pytest.raises(RadioError, match="markup"):
        parse_radio_history("<html>upstream maintenance</html>")


def nostalgie_track():
    return {
        "id": 9586963,
        "radio_id": 9,
        "artist": " Bee Gees ",
        "title": " Night Fever ",
        "played_at": "2026-11-23 14:50:57",
    }


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        ("2026-11-23 14:50:57", "2026-11-23T13:50:57+00:00"),
        ("2026-07-23 14:50:57", "2026-07-23T12:50:57+00:00"),
        ("2026-10-25 02:30:00", "2026-10-25T00:30:00+00:00"),
        ("2026-11-23T14:50:57+00:00", "2026-11-23T14:50:57+00:00"),
    ],
)
def test_nostalgie_history_converts_station_timestamps(timestamp, expected):
    track = {**nostalgie_track(), "played_at": timestamp}
    entries = parse_nostalgie_history(
        {"data": [track, {"artist": "", "title": "News"}]}
    )
    assert entries == [RadioPlay(expected, "Bee Gees", "Night Fever")]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": None},
        {"data": [None]},
        {"data": [{**nostalgie_track(), "played_at": None}]},
        {"data": [{**nostalgie_track(), "played_at": "not a timestamp"}]},
        {"data": [{**nostalgie_track(), "played_at": "2026-03-29 02:30:00"}]},
    ],
)
def test_nostalgie_malformed_history_is_an_error(payload):
    with pytest.raises(RadioError):
        parse_nostalgie_history(payload)


async def test_nostalgie_fetch_queues_and_downloads_using_spotify_search(
    monitor, tmp_path, mocker
):
    enable(monitor, "nostalgie")
    response = mocker.Mock()
    response.json.return_value = {"data": [nostalgie_track()]}
    get = mocker.patch.object(radio.requests, "get", return_value=response)
    song = SimpleNamespace(name="Night Fever", artist="Bee Gees", download_url=None)
    search = mocker.patch.object(radio, "get_search_results", return_value=[song])
    connection = mocker.Mock()
    connection.contains.return_value = False
    mocker.patch.object(monitor, "navidrome", return_value=connection)
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"test audio")
    monitor.downloader = mocker.Mock()
    monitor.downloader.pool_download = mocker.AsyncMock(return_value=(song, audio))

    await monitor.poll_station("nostalgie")
    await monitor.poll_station("nostalgie")
    rows = monitor.view()["plays"]
    assert len(rows) == 1
    await monitor.process_play(rows[0])

    get.assert_called_with(
        "https://mediahuis.pointbreak.dev/api/v1/radio/9/tracks-paginated",
        headers={"User-Agent": "spotdl-radio-monitor"},
        timeout=20,
    )
    assert get.call_count == 2
    connection.contains.assert_called_once_with("Bee Gees", "Night Fever")
    search.assert_called_once_with("Bee Gees Night Fever", limit=5)
    monitor.downloader.pool_download.assert_awaited_once_with(song)
    assert monitor.view()["plays"][0]["status"] == "downloaded"


def test_existing_database_gains_nostalgie_without_losing_station_state(
    monitor, tmp_path
):
    enable(monitor, "stubru")
    with monitor.db:
        monitor.db.execute("DELETE FROM stations WHERE id='nostalgie'")
    resumed = RadioMonitor(
        tmp_path / "radio.sqlite3", DownloaderOptions(**DOWNLOADER_OPTIONS)
    )
    try:
        stations = {station["id"]: station for station in resumed.view()["stations"]}
        assert stations["stubru"]["enabled"]
        assert not stations["nostalgie"]["enabled"]
    finally:
        resumed.db.close()


def willy_track():
    return {
        "played_at": "2026-11-23T12:05:00+01:00",
        "artist": {"name": "Artist"},
        "title": "Song",
        "spotify_url": "http://open.spotify.com/track/0123456789012345678901",
        "videos": [{"type": "youtube", "id": "i5Tiqv4Irjs"}],
    }


def test_willy_history_handles_optional_links_and_normalizes_timestamp():
    entry = willy_track()
    parsed = parse_willy_history({"played_tracks": [entry]})[0]
    assert parsed.played_at == "2026-11-23T11:05:00+00:00"
    assert parsed.spotify_url == (
        "https://open.spotify.com/track/0123456789012345678901"
    )
    assert parsed.youtube_url == "https://www.youtube.com/watch?v=i5Tiqv4Irjs"

    entry.update(spotify_url=None, videos=None)
    parsed = parse_willy_history({"played_tracks": [entry]})[0]
    assert not parsed.spotify_url
    assert not parsed.youtube_url

    entry["videos"] = [{"type": "youtube", "id": "invalid/../id"}]
    assert not parse_willy_history({"played_tracks": [entry]})[0].youtube_url


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"played_tracks": None},
        {"played_tracks": [None]},
        {"played_tracks": [{**willy_track(), "played_at": None}]},
        {"played_tracks": [{**willy_track(), "played_at": "2026-11-23T12:05:00"}]},
    ],
)
def test_willy_malformed_history_is_an_error(payload):
    with pytest.raises(RadioError):
        parse_willy_history(payload)


@pytest.mark.parametrize(
    ("station", "api_id"),
    [("willy", "willy_be"), ("willy-classic", "willy_be_class_x")],
)
async def test_willy_api_uses_spotify_and_ignores_station_video(
    monitor, tmp_path, mocker, station, api_id
):
    enable(monitor, station)
    response = mocker.Mock()
    response.json.return_value = {"played_tracks": [willy_track()]}
    get = mocker.patch.object(radio.requests, "get", return_value=response)
    song = SimpleNamespace(name="Song", artist="Artist", download_url=None)
    metadata = mocker.patch.object(radio.Song, "from_url", return_value=song)
    search = mocker.patch.object(radio, "get_search_results")
    connection = mocker.Mock()
    connection.contains.return_value = False
    mocker.patch.object(monitor, "navidrome", return_value=connection)
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"test audio")
    monitor.downloader = mocker.Mock()

    async def download(selected):
        assert selected.download_url is None
        return selected, audio

    monitor.downloader.pool_download = mocker.AsyncMock(side_effect=download)
    await monitor.poll_station(station)
    await monitor.process_play(monitor.view()["plays"][0])

    get.assert_called_once_with(
        f"https://api.willy.radio/2.0/tracks/plays?limit=30&_station_id={api_id}",
        headers={"User-Agent": "spotdl-radio-monitor"},
        timeout=20,
    )
    response.raise_for_status.assert_called_once()
    connection.contains.assert_called_once_with("Artist", "Song")
    metadata.assert_called_once_with(
        "https://open.spotify.com/track/0123456789012345678901"
    )
    search.assert_not_called()
    assert monitor.view()["plays"][0]["status"] == "downloaded"


def test_existing_database_gains_youtube_column_without_losing_history(
    monitor, tmp_path
):
    queue_play(monitor)
    with monitor.db:
        monitor.db.execute("ALTER TABLE plays DROP COLUMN youtube_url")
    resumed = RadioMonitor(
        tmp_path / "radio.sqlite3", DownloaderOptions(**DOWNLOADER_OPTIONS)
    )
    try:
        row = resumed.view()["plays"][0]
        assert row["title"] == "Song"
        assert row["status"] == "queued"
        assert row["youtube_url"] == ""
    finally:
        resumed.db.close()


async def test_first_history_and_only_unseen_plays_are_queued(monitor, monkeypatch):
    enable(monitor, "stubru")
    history = [play("Older")]
    monkeypatch.setattr(radio, "fetch_radio_history", lambda station: history)
    await monitor.poll_station("stubru")
    assert [row["status"] for row in monitor.view()["plays"]] == ["queued"]

    history.insert(0, play("New", 5))
    await monitor.poll_station("stubru")
    await monitor.poll_station("stubru")
    assert [(row["title"], row["status"]) for row in monitor.view()["plays"]] == [
        ("New", "queued"),
        ("Older", "queued"),
    ]


async def test_seen_history_and_queue_survive_restart(monitor, tmp_path, monkeypatch):
    enable(monitor, "stubru")
    history = [play()]
    monkeypatch.setattr(radio, "fetch_radio_history", lambda station: history)
    await monitor.poll_station("stubru")
    history.insert(0, play("New", 5))
    await monitor.poll_station("stubru")
    with monitor.db:
        monitor.db.execute(
            "UPDATE plays SET status='downloading' WHERE status='queued'"
        )
    monitor.db.close()
    resumed = RadioMonitor(
        tmp_path / "radio.sqlite3", DownloaderOptions(**DOWNLOADER_OPTIONS)
    )
    try:
        await resumed.poll_station("stubru")
        assert [row["status"] for row in resumed.view()["plays"]] == [
            "queued",
            "queued",
        ]
    finally:
        resumed.db.close()


async def test_legacy_baseline_is_queued_on_restart(monitor, tmp_path):
    queue_play(monitor)
    with monitor.db:
        monitor.db.execute("UPDATE plays SET status='baseline'")
    resumed = RadioMonitor(
        tmp_path / "radio.sqlite3", DownloaderOptions(**DOWNLOADER_OPTIONS)
    )
    try:
        assert resumed.view()["plays"][0]["status"] == "queued"
    finally:
        resumed.db.close()


@pytest.mark.parametrize("collected", [True, False])
async def test_first_history_checks_navidrome_before_downloading(
    monitor, tmp_path, mocker, collected
):
    enable(monitor, "stubru")
    mocker.patch.object(radio, "fetch_radio_history", return_value=[play()])
    connection = mocker.Mock()
    connection.contains.return_value = collected
    mocker.patch.object(monitor, "navidrome", return_value=connection)
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"test audio")
    download = mocker.patch.object(monitor, "download", return_value=audio)

    await monitor.poll_station("stubru")
    await monitor.process_play(monitor.view()["plays"][0])

    connection.contains.assert_called_once_with("Artist", "Song")
    assert monitor.view()["plays"][0]["status"] == (
        "already collected" if collected else "downloaded"
    )
    if collected:
        download.assert_not_called()
    else:
        download.assert_awaited_once()


async def test_existing_song_is_already_collected(monitor, mocker):
    connection = mocker.Mock()
    connection.contains.return_value = True
    mocker.patch.object(monitor, "navidrome", return_value=connection)
    download = mocker.patch.object(monitor, "download")
    row = queue_play(monitor)

    await monitor.process_play(row)

    assert monitor.view()["plays"][0]["status"] == "already collected"
    connection.contains.assert_called_once_with("Artist", "Song")
    download.assert_not_called()


async def test_library_failure_blocks_download_and_can_be_retried(monitor, mocker):
    connection = mocker.Mock()
    connection.contains.side_effect = NavidromeError("Unavailable")
    mocker.patch.object(monitor, "navidrome", return_value=connection)
    download = mocker.patch.object(monitor, "download")
    row = queue_play(monitor)

    await monitor.process_play(row)
    assert monitor.view()["plays"][0]["status"] == "retry"
    assert monitor.view()["plays"][0]["attempts"] == 1
    assert "Unavailable" in monitor.view()["plays"][0]["message"]
    download.assert_not_called()

    mocker.patch.object(radio, "fetch_radio_history", return_value=[play()])
    await monitor.poll_station("stubru")
    row = monitor.view()["plays"][0]
    assert row["status"] == "retry"
    connection.contains.side_effect = None
    connection.contains.return_value = True
    await monitor.process_play(row)
    assert monitor.view()["plays"][0]["status"] == "already collected"


async def test_retries_follow_normal_queue_and_stop_until_next_play(
    monitor, tmp_path, mocker
):
    enable(monitor, "stubru")
    connection = mocker.Mock()
    connection.contains.return_value = False
    mocker.patch.object(monitor, "navidrome", return_value=connection)
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"test audio")
    calls = []
    finished = asyncio.Event()

    async def download(entry):
        calls.append(entry.title)
        if entry.title == "Failed" and calls.count("Failed") <= 2:
            raise RadioError("Unavailable")
        finished.set()
        return audio

    mocker.patch.object(monitor, "download", side_effect=download)
    queue_play(monitor, entry=play("Failed"))
    queue_play(monitor, entry=play("Normal", 1))
    task = asyncio.create_task(monitor.download_loop())
    try:

        async def wait_for_failed():
            while (
                monitor.db.execute(
                    "SELECT status FROM plays WHERE title='Failed'"
                ).fetchone()["status"]
                != "failed"
            ):
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_failed(), timeout=2)
        assert calls == ["Failed", "Normal", "Failed"]
        row = monitor.db.execute(
            "SELECT status, attempts FROM plays WHERE title='Failed'"
        ).fetchone()
        assert (row["status"], row["attempts"]) == ("failed", 2)

        mocker.patch.object(radio, "fetch_radio_history", return_value=[play("Failed")])
        await monitor.poll_station("stubru")
        await asyncio.sleep(0.01)
        assert calls == ["Failed", "Normal", "Failed"]

        finished.clear()
        mocker.patch.object(
            radio, "fetch_radio_history", return_value=[play("Failed", 2)]
        )
        await monitor.poll_station("stubru")
        await asyncio.wait_for(finished.wait(), timeout=2)
        assert calls == ["Failed", "Normal", "Failed", "Failed"]
        assert (
            monitor.db.execute(
                "SELECT status FROM plays WHERE title='Failed' ORDER BY id DESC"
            ).fetchone()["status"]
            == "downloaded"
        )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_retry_and_interrupted_retry_survive_restart(monitor, tmp_path, mocker):
    enable(monitor, "stubru")
    connection = mocker.Mock()
    connection.contains.side_effect = NavidromeError("Unavailable")
    mocker.patch.object(monitor, "navidrome", return_value=connection)
    await monitor.process_play(queue_play(monitor))
    with monitor.db:
        monitor.db.execute("UPDATE plays SET status='checking' WHERE status='retry'")

    resumed = RadioMonitor(
        tmp_path / "radio.sqlite3", DownloaderOptions(**DOWNLOADER_OPTIONS)
    )
    try:
        row = resumed.view()["plays"][0]
        assert (row["status"], row["attempts"]) == ("retry", 1)
        mocker.patch.object(resumed, "navidrome", return_value=connection)
        await resumed.process_play(row)
        row = resumed.view()["plays"][0]
        assert (row["status"], row["attempts"]) == ("failed", 2)
    finally:
        resumed.db.close()


async def test_retry_can_succeed_without_another_source_poll(monitor, tmp_path, mocker):
    enable(monitor, "stubru")
    connection = mocker.Mock()
    connection.contains.return_value = False
    mocker.patch.object(monitor, "navidrome", return_value=connection)
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"test audio")
    download = mocker.patch.object(
        monitor, "download", side_effect=[RadioError("Unavailable"), audio]
    )
    await monitor.process_play(queue_play(monitor))
    await monitor.process_play(monitor.view()["plays"][0])
    assert monitor.view()["plays"][0]["status"] == "downloaded"
    assert download.await_count == 2


async def test_legacy_failed_play_gets_one_retry(monitor, tmp_path):
    queue_play(monitor)
    with monitor.db:
        monitor.db.execute("UPDATE plays SET status='failed', message='Old error'")
        monitor.db.execute("ALTER TABLE plays DROP COLUMN attempts")
    resumed = RadioMonitor(
        tmp_path / "radio.sqlite3", DownloaderOptions(**DOWNLOADER_OPTIONS)
    )
    try:
        row = resumed.view()["plays"][0]
        assert (row["status"], row["attempts"], row["message"]) == (
            "retry",
            1,
            "Old error",
        )
    finally:
        resumed.db.close()


async def test_same_song_on_two_stations_is_downloaded_only_once(
    monitor, tmp_path, mocker
):
    enable(monitor, "stubru", "radio1")
    connection = mocker.Mock()
    connection.contains.return_value = False
    mocker.patch.object(monitor, "navidrome", return_value=connection)
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"test audio")
    download = mocker.patch.object(monitor, "download", return_value=audio)
    first = queue_play(monitor, "stubru")
    second = queue_play(monitor, "radio1")

    await monitor.process_play(first)
    await monitor.process_play(second)

    assert {row["status"] for row in monitor.view()["plays"]} == {
        "downloaded",
        "already collected",
    }
    download.assert_awaited_once()


async def test_paused_source_does_not_start_a_download(monitor, mocker):
    connection = mocker.Mock()
    connection.contains.return_value = False
    mocker.patch.object(monitor, "navidrome", return_value=connection)
    download = mocker.patch.object(monitor, "download")
    await monitor.process_play(queue_play(monitor))
    assert monitor.view()["plays"][0]["status"] == "queued"
    download.assert_not_called()


async def test_download_uses_source_spotify_id_and_requires_an_audio_file(
    monitor, tmp_path, mocker
):
    song = SimpleNamespace(name="Song", artist="Artist")
    metadata = mocker.patch.object(radio.Song, "from_url", return_value=song)
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"test audio")
    downloader = mocker.Mock()
    downloader.pool_download = mocker.AsyncMock(return_value=(song, audio))
    monitor.downloader = downloader
    entry = RadioPlay(
        play().played_at, "Artist", "Song", "https://open.spotify.com/track/id"
    )

    assert await monitor.download(entry) == audio
    metadata.assert_called_once_with(entry.spotify_url)
    downloader.pool_download.assert_awaited_once_with(song)
    audio.unlink()
    with pytest.raises(RadioError, match="audio file"):
        await monitor.download(entry)


async def test_download_without_spotify_id_rejects_unrelated_recordings(
    monitor, mocker
):
    mocker.patch.object(
        radio,
        "get_search_results",
        return_value=[SimpleNamespace(name="Song", artist="Unrelated Artist")],
    )
    with pytest.raises(RadioError, match="matching Spotify"):
        await monitor.download(play())
    assert monitor.downloader is None


async def test_station_video_is_ignored_when_spotify_link_is_missing(
    monitor, tmp_path, mocker
):
    song = SimpleNamespace(name="Song", artist="Artist", download_url=None)
    search = mocker.patch.object(radio, "get_search_results", return_value=[song])
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"test audio")
    monitor.downloader = mocker.Mock()
    monitor.downloader.pool_download = mocker.AsyncMock(return_value=(song, audio))
    entry = RadioPlay(
        play().played_at, "Artist", "Song", youtube_url="https://youtu.be/i5Tiqv4Irjs"
    )

    assert await monitor.download(entry) == audio
    search.assert_called_once_with("Artist Song", limit=5)
    assert song.download_url is None


async def test_classical_source_searches_by_composer_without_performer_notes(
    monitor, tmp_path, mocker
):
    song = SimpleNamespace(name="Composition", artist="Composer")
    search = mocker.patch.object(radio, "get_search_results", return_value=[song])
    audio = tmp_path / "composition.mp3"
    audio.write_bytes(b"test audio")
    monitor.downloader = mocker.Mock()
    monitor.downloader.pool_download = mocker.AsyncMock(return_value=(song, audio))
    entry = RadioPlay(
        play().played_at, "Composer | Uitvoerder: Ensemble", "Composition"
    )

    assert await monitor.download(entry) == audio
    search.assert_called_once_with("Composer Composition", limit=5)


async def test_failed_connection_does_not_replace_saved_settings(monitor, mocker):
    ping = mocker.patch.object(Navidrome, "ping")
    await monitor.configure(
        ["stubru"], "https://music.example", "user", "test-password"
    )
    ping.side_effect = NavidromeError("Invalid credentials")
    with pytest.raises(NavidromeError):
        await monitor.configure(["radio1"], "https://other.example", "other", "bad")

    state = monitor.view()
    assert [station["id"] for station in state["stations"] if station["enabled"]] == [
        "stubru"
    ]
    assert state["navidrome_url"] == "https://music.example"
    assert "test-password" not in repr(state)


async def test_saved_password_is_not_reused_for_another_server(monitor, mocker):
    ping = mocker.patch.object(Navidrome, "ping")
    await monitor.configure(
        ["stubru"], "https://music.example", "user", "test-password"
    )
    await monitor.configure(["radio1"], "https://music.example", "user", "")
    assert ping.call_count == 2
    with pytest.raises(ValueError, match="password"):
        await monitor.configure(["radio1"], "https://other.example", "user", "")
    assert ping.call_count == 2


async def test_stations_can_be_paused_when_navidrome_is_unavailable(monitor, mocker):
    ping = mocker.patch.object(Navidrome, "ping")
    await monitor.configure(
        ["stubru"], "https://music.example", "user", "test-password"
    )
    ping.side_effect = NavidromeError("Offline")
    await monitor.configure([], "https://music.example", "user", "")

    assert not any(station["enabled"] for station in monitor.view()["stations"])
    assert ping.call_count == 1


async def test_disabling_and_reenabling_queues_current_history(monitor, mocker):
    mocker.patch.object(Navidrome, "ping")
    mocker.patch.object(radio, "fetch_radio_history", return_value=[play()])
    await monitor.configure(
        ["stubru"], "https://music.example", "user", "test-password"
    )
    await monitor.poll_station("stubru")
    await monitor.configure([], "https://music.example", "user", "")
    await monitor.configure(["stubru"], "https://music.example", "user", "")
    mocker.patch.object(radio, "fetch_radio_history", return_value=[play("New", 5)])
    await monitor.poll_station("stubru")
    assert len(monitor.view()["plays"]) == 2
    assert all(row["status"] == "queued" for row in monitor.view()["plays"])


async def test_poller_waits_thirty_minutes_but_can_be_woken(monitor, monkeypatch):
    enable(monitor, "stubru")
    clock = [1_000_000.0]
    monkeypatch.setattr(radio, "time", SimpleNamespace(time=lambda: clock[0]))
    polled = asyncio.Event()
    calls = []

    async def poll(station):
        calls.append(station)
        with monitor.db:
            monitor.db.execute("UPDATE stations SET checked_at=?", (clock[0],))
        polled.set()

    monkeypatch.setattr(monitor, "poll_station", poll)
    task = asyncio.create_task(monitor.poll_loop())
    try:
        await asyncio.wait_for(polled.wait(), timeout=1)
        polled.clear()
        monitor.wake.set()
        await asyncio.sleep(0.01)
        assert calls == ["stubru"]
        clock[0] += 1800
        monitor.wake.set()
        await asyncio.wait_for(polled.wait(), timeout=1)
        assert calls == ["stubru", "stubru"]
        polled.clear()
        monitor.check_now()
        await asyncio.wait_for(polled.wait(), timeout=1)
        assert calls == ["stubru", "stubru", "stubru"]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.parametrize(
    ("now", "start", "end", "upcoming"),
    [
        (
            "2026-11-23T01:59:59+00:00",
            "2026-11-21T02:00:00+00:00",
            "2026-11-22T02:00:00+00:00",
            "2026-11-23T02:00:00+00:00",
        ),
        (
            "2026-03-29T01:00:00+00:00",
            "2026-03-28T02:00:00+00:00",
            "2026-03-29T01:00:00+00:00",
            "2026-03-30T01:00:00+00:00",
        ),
        (
            "2026-10-25T02:00:00+00:00",
            "2026-10-24T01:00:00+00:00",
            "2026-10-25T02:00:00+00:00",
            "2026-10-26T02:00:00+00:00",
        ),
    ],
)
def test_playlist_day_uses_brussels_three_am_including_dst(now, start, end, upcoming):
    assert playlist_window(datetime.fromisoformat(now)) == tuple(
        datetime.fromisoformat(value) for value in (start, end, upcoming)
    )


async def test_daily_playlists_deduplicate_replace_and_only_include_monitored_day(
    monitor, mocker
):
    enable(monitor, "stubru", "willy", "klara")
    queue_play(monitor)
    queue_play(monitor, entry=play(minute=5))
    queue_play(monitor, source="willy")
    queue_play(monitor, source="willy", entry=play("Other", 10))
    queue_play(monitor, source="willy", entry=play("Not indexed", 15))
    queue_play(monitor, source="radio1", entry=play("Disabled"))
    queue_play(
        monitor, entry=RadioPlay("2026-11-23T01:59:59+00:00", "Artist", "Too old")
    )
    queue_play(
        monitor, entry=RadioPlay("2026-11-24T02:00:00+00:00", "Artist", "Too new")
    )
    connection = Navidrome("https://music.example", "user", "password")
    mocker.patch.object(monitor, "navidrome", return_value=connection)
    lookup = mocker.patch.object(
        Navidrome,
        "find_song_id",
        side_effect=lambda artist, title: {"Song": "one", "Other": "two"}.get(title),
    )
    remote = {
        "existing": {
            "id": "existing",
            "name": "spotDL Radio - Studio Brussel",
            "owner": "user",
            "songs": ["old"],
        },
        "unrelated": {
            "id": "unrelated",
            "name": "Personal playlist",
            "owner": "user",
            "songs": ["keep"],
        },
    }

    def request(_connection, method, **parameters):
        if method == "getPlaylists":
            return {"playlists": {"playlist": list(remote.values())}}
        assert method == "createPlaylist"
        ids = parameters["songId"]
        assert len(ids) == len(set(ids))
        playlist_id = parameters.get("playlistId", str(len(remote)))
        if playlist_id not in remote:
            remote[playlist_id] = {
                "id": playlist_id,
                "name": parameters["name"],
                "owner": "user",
            }
        remote[playlist_id]["songs"] = ids
        return {"playlist": {"id": playlist_id}}

    mocker.patch.object(Navidrome, "request", autospec=True, side_effect=request)
    start = datetime(2026, 11, 23, 2, tzinfo=timezone.utc)
    end = datetime(2026, 11, 24, 2, tzinfo=timezone.utc)
    await monitor.publish_playlists(start, end)

    assert remote["existing"]["songs"] == ["one"]
    assert remote["unrelated"]["songs"] == ["keep"]
    by_name = {value["name"]: value["songs"] for value in remote.values()}
    assert by_name["spotDL Radio - Willy"] == ["one", "two"]
    assert by_name["spotDL Radio - Klara"] == []
    assert by_name["spotDL Radio - Super Mix"] == ["one", "two"]
    assert len(remote) == 5
    assert lookup.call_count == 3
    assert "Not indexed" in monitor.view()["output"]

    await monitor.publish_playlists(end, datetime(2026, 11, 25, 2, tzinfo=timezone.utc))
    assert len(remote) == 5
    assert remote["existing"]["songs"] == []


async def test_playlist_library_failure_preserves_previous_playlists(monitor, mocker):
    enable(monitor, "stubru")
    queue_play(monitor)
    connection = mocker.Mock()
    connection.find_song_id.side_effect = NavidromeError("Search unavailable")
    mocker.patch.object(monitor, "navidrome", return_value=connection)

    with pytest.raises(NavidromeError):
        await monitor.publish_playlists(
            datetime(2026, 11, 23, 2, tzinfo=timezone.utc),
            datetime(2026, 11, 24, 2, tzinfo=timezone.utc),
        )
    connection.replace_playlist.assert_not_called()


async def test_playlists_use_full_daily_history_not_the_ui_limit(monitor, mocker):
    enable(monitor, "stubru")
    start = datetime(2026, 11, 23, 2, tzinfo=timezone.utc)
    for second in range(301):
        queue_play(
            monitor,
            entry=RadioPlay(
                (start + timedelta(seconds=second)).isoformat(),
                "Artist",
                "Outside UI limit" if second == 0 else "Song",
            ),
        )
    assert len(monitor.view()["plays"]) == 300
    connection = mocker.Mock()
    connection.find_song_id.side_effect = lambda artist, title: title
    mocker.patch.object(monitor, "navidrome", return_value=connection)

    await monitor.publish_playlists(start, start + timedelta(days=1))

    assert connection.find_song_id.call_count == 2
    assert set(connection.replace_playlist.call_args_list[0].args[1]) == {
        "Outside UI limit",
        "Song",
    }


async def test_playlist_schedule_runs_once_at_three_and_catches_up_after_restart(
    monitor, tmp_path, mocker
):
    enable(monitor, "stubru")
    before = datetime(2026, 11, 23, 1, 59, tzinfo=timezone.utc).timestamp()
    due = datetime(2026, 11, 23, 2, tzinfo=timezone.utc).timestamp()
    publish = mocker.patch.object(monitor, "publish_playlists")
    await monitor.check_playlists(before)
    await monitor.check_playlists(due - 1)
    publish.assert_not_called()
    await monitor.check_playlists(due)
    await monitor.check_playlists(due + 60)
    publish.assert_awaited_once_with(
        datetime(2026, 11, 22, 2, tzinfo=timezone.utc),
        datetime(2026, 11, 23, 2, tzinfo=timezone.utc),
    )

    resumed = RadioMonitor(
        tmp_path / "radio.sqlite3", DownloaderOptions(**DOWNLOADER_OPTIONS)
    )
    try:
        publish = mocker.patch.object(resumed, "publish_playlists")
        await resumed.check_playlists(due + 120)
        publish.assert_not_called()
        await resumed.check_playlists(
            datetime(2026, 11, 26, 12, tzinfo=timezone.utc).timestamp()
        )
        publish.assert_awaited_once_with(
            datetime(2026, 11, 25, 2, tzinfo=timezone.utc),
            datetime(2026, 11, 26, 2, tzinfo=timezone.utc),
        )
    finally:
        resumed.db.close()


async def test_playlist_errors_retry_without_marking_run_successful(monitor, mocker):
    enable(monitor, "stubru")
    due = datetime(2026, 11, 23, 2, tzinfo=timezone.utc).timestamp()
    await monitor.check_playlists(due - 60)
    publish = mocker.patch.object(
        monitor, "publish_playlists", side_effect=[NavidromeError("Offline"), None]
    )

    await monitor.check_playlists(due)
    assert monitor.view()["playlists"]["error"] == "Offline"
    assert not monitor.view()["playlists"]["last_run"]
    await monitor.check_playlists(due + 299)
    assert publish.await_count == 1
    await monitor.check_playlists(due + 300)
    assert publish.await_count == 2
    assert not monitor.view()["playlists"]["error"]
    assert monitor.view()["playlists"]["last_run"] == "2026-11-23T02:00:00+00:00"


async def test_no_monitored_stations_pause_playlists(monitor, mocker):
    publish = mocker.patch.object(monitor, "publish_playlists")
    await monitor.check_playlists(
        datetime(2026, 11, 23, 2, tzinfo=timezone.utc).timestamp()
    )
    publish.assert_not_called()
    assert monitor.view()["playlists"]["next_run"] == 0


@pytest.mark.parametrize("use_output_dir", [True, False])
def test_radio_and_regular_web_downloads_share_destination(
    tmp_path, monkeypatch, use_output_dir
):
    monkeypatch.setattr(web_utils, "get_spotdl_path", lambda: tmp_path)
    settings = DownloaderOptions(**DOWNLOADER_OPTIONS)
    settings["output"] = str(tmp_path / "music" / "{artist}" / "{title}.{output-ext}")
    instance = RadioMonitor(tmp_path / "radio.sqlite3", settings, use_output_dir)
    try:
        expected = (
            settings["output"]
            if use_output_dir
            else str(tmp_path / "web" / "downloads")
        )
        assert instance.settings["output"] == expected
        assert web_utils.get_web_download_output(settings, use_output_dir) == expected
    finally:
        instance.db.close()


def test_optional_navidrome_only_skips_completely_unconfigured_connection(monitor):
    assert monitor.optional_navidrome() is None
    with monitor.db:
        monitor.db.execute("UPDATE settings SET url='https://music.example' WHERE id=1")
    with pytest.raises(ValueError, match="username and password"):
        monitor.optional_navidrome()
