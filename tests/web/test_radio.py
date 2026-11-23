import logging
from urllib.parse import urlencode

import pytest
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request

from spotdl.types.options import DownloaderOptions
from spotdl.utils.config import DOWNLOADER_OPTIONS
from spotdl.utils.navidrome import Navidrome
from spotdl.utils.radio import RadioMonitor
from spotdl.utils.web import app_state
from spotdl.web import radio
from tests.web.test_datastar import call_asgi


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    instance = RadioMonitor(
        tmp_path / "radio.sqlite3", DownloaderOptions(**DOWNLOADER_OPTIONS)
    )
    monkeypatch.setattr(app_state, "radio_monitor", instance)
    monkeypatch.setattr(
        app_state, "logger", logging.getLogger("radio-test"), raising=False
    )
    yield instance
    instance.db.close()


def request(path="/radio", origin=None):
    headers = [(b"host", b"localhost:8800")]
    if origin:
        headers.append((b"origin", origin.encode()))
    return Request(
        {
            "type": "http",
            "scheme": "http",
            "method": "POST",
            "path": path,
            "query_string": b"",
            "headers": headers,
            "server": ("localhost", 8800),
        }
    )


async def test_radio_page_lists_stations_without_password(monitor, mocker):
    mocker.patch.object(Navidrome, "ping")
    await monitor.configure(
        ["stubru"], "https://music.example", "user", "test-password"
    )
    response = await radio.radio_page(request())

    html = response.body.decode()
    assert html.count('name="stations"') == 12
    assert 'value="willy"' in html
    assert 'value="willy-classic"' in html
    assert 'value="nostalgie"' in html
    assert "https://mediahuis.pointbreak.dev/api/v1/radio/9/tracks-paginated" in html
    assert "https://api.willy.radio/" in html
    assert 'href="/radio"' in html
    assert "https://music.example" in html
    assert "test-password" not in html
    assert "once every 10 minutes" in html
    assert "2, 4, 8, 16, and 32 minutes" in html
    assert "Download destination" not in html
    assert "Nightly Navidrome playlists" not in html


async def test_settings_validate_and_redirect(monitor, mocker):
    ping = mocker.patch.object(Navidrome, "ping")
    app = FastAPI()
    app.include_router(radio.router)
    status, headers, _ = await call_asgi(
        app,
        "/radio/settings",
        method="POST",
        request_body=urlencode(
            [
                ("stations", "stubru"),
                ("stations", "radio1"),
                ("navidrome_url", "https://music.example"),
                ("navidrome_username", "user"),
                ("navidrome_password", "test-password"),
            ]
        ).encode(),
        request_headers=(
            (b"host", b"localhost:8800"),
            (b"origin", b"http://localhost:8800"),
            (b"content-type", b"application/x-www-form-urlencoded"),
        ),
    )

    assert status == 303
    assert headers[b"location"] == b"/radio?saved=1"
    ping.assert_called_once()
    assert monitor.wake.is_set()
    assert {
        station["id"] for station in monitor.view()["stations"] if station["enabled"]
    } == {"stubru", "radio1"}


async def test_cross_origin_settings_are_rejected(monitor):
    with pytest.raises(HTTPException) as error:
        await radio.save_settings(
            request("/radio/settings", "https://other.example"),
            [],
            "",
            "",
            "",
        )
    assert error.value.status_code == 403


async def test_invalid_settings_preserve_non_secret_form_values(monitor, mocker):
    ping = mocker.patch.object(
        Navidrome, "ping", side_effect=ValueError("Invalid connection")
    )
    response = await radio.save_settings(
        request("/radio/settings"),
        ["stubru"],
        "https://music.example",
        "user",
        "test-password",
    )

    assert response.status_code == 400
    ping.assert_called_once()
    html = response.body.decode()
    assert 'value="https://music.example"' in html
    assert 'value="user"' in html
    assert 'value="stubru" checked' in html
    assert "test-password" not in html
    assert not any(station["enabled"] for station in monitor.view()["stations"])


async def test_live_output_escapes_upstream_content(monitor):
    monitor.append_log("ERROR <script>not executable</script>")
    response = await radio.updates()
    stream = response.body_iterator
    try:
        output = await anext(stream)
    finally:
        await stream.aclose()
    assert "&lt;script&gt;not executable&lt;/script&gt;" in output
    assert "Nightly Navidrome playlists" not in output


async def test_radio_station_filter_lists_only_monitored_stations(monitor):
    with monitor.db:
        monitor.db.execute("UPDATE stations SET enabled=1 WHERE id='stubru'")
        monitor.db.executemany(
            """INSERT INTO plays
            (station, played_at, artist, title, spotify_url, song_key, status)
            VALUES (?, '2026-11-23T12:00:00+00:00', 'Artist', ?, '', ?, 'queued')""",
            [("stubru", "First", "first"), ("radio1", "Second", "second")],
        )
    response = await radio.radio_page(request())
    page = BeautifulSoup(response.body, "html.parser")
    select = page.select_one("#radio-station-filter")
    assert select["data-bind"] == "radio_station_filter"
    assert [(option["value"], option.text) for option in select.select("option")][
        :3
    ] == [("", "All stations"), ("radio1", "Radio 1"), ("radio2", "Radio 2")]
    assert select.select_one('option[value="radio1"]').has_attr("hidden")
    assert select.select_one('option[value="radio1"]').has_attr("disabled")
    assert not select.select_one('option[value="stubru"]').has_attr("hidden")
    first = page.find(string="Artist - First").find_parent("tr")
    second = page.find(string="Artist - Second").find_parent("tr")
    assert '$radio_station_filter === "stubru"' in first["data-show"]
    assert '$radio_station_filter === "radio1"' in second["data-show"]
    empty = page.find(string="No recent plays for Radio 2.").find_parent("tr")
    assert empty["data-show"] == '$radio_station_filter === "radio2"'
    assert "No recent plays for Radio 1." not in page.get_text()


async def test_radio_updates_send_enabled_stations(monitor):
    response = await radio.updates()
    stream = response.body_iterator
    try:
        await anext(stream)
        assert '"radio_enabled_stations":"||"' in await anext(stream)
        with monitor.db:
            monitor.db.execute("UPDATE stations SET enabled=1 WHERE id='stubru'")
        await anext(stream)
        assert '"radio_enabled_stations":"|stubru|"' in await anext(stream)
    finally:
        await stream.aclose()


@pytest.mark.parametrize(
    ("status", "attempts", "expected"),
    [
        ("queued", 0, "Next attempt"),
        ("retry", 2, "Next attempt"),
        ("downloading", 2, "Attempt"),
        ("downloaded", 2, "Attempt"),
        ("failed", 6, "Attempt"),
    ],
)
async def test_radio_activity_shows_attempt_number(monitor, status, attempts, expected):
    with monitor.db:
        monitor.db.execute(
            """INSERT INTO plays
            (station, played_at, artist, title, spotify_url, song_key, status,
             attempts, retry_at) VALUES
            ('stubru', '2026-11-23T12:00:00+00:00', 'Artist', 'Song', '', 'key', ?, ?, ?)""",
            (status, attempts, 1795472400),
        )
    response = await radio.radio_page(request())
    html = response.body.decode()
    number = attempts if status == "failed" else attempts + 1
    assert f"{expected}\n                                {number} / 6" in html
    assert ("Retry after " in html) == (status == "retry")


async def test_router_lifespan_starts_and_stops_monitor(tmp_path, monkeypatch):
    monkeypatch.setattr(radio, "get_spotdl_path", lambda: tmp_path)
    monkeypatch.setattr(
        app_state,
        "downloader_settings",
        DownloaderOptions(**DOWNLOADER_OPTIONS),
        raising=False,
    )
    monkeypatch.setattr(app_state, "web_settings", {}, raising=False)
    app = FastAPI()
    app.include_router(radio.router)
    async with app.router.lifespan_context(app):
        monitor = app_state.radio_monitor
        assert monitor is not None
        assert len(monitor.tasks) == 3
    assert app_state.radio_monitor is None
    assert all(task.cancelled() for task in monitor.tasks)


async def test_router_uses_persistent_config_directory_and_environment(
    tmp_path, monkeypatch, mocker
):
    directory = tmp_path / "config"
    monkeypatch.setenv("SPOTDL_RADIO_CONFIG_DIR", str(directory))
    monkeypatch.setenv("NAVIDROME_URL", "https://music.example")
    monkeypatch.setenv("NAVIDROME_USERNAME", "user")
    monkeypatch.setenv("NAVIDROME_PASSWORD", "test-password")
    monkeypatch.setattr(
        app_state,
        "downloader_settings",
        DownloaderOptions(**DOWNLOADER_OPTIONS),
        raising=False,
    )
    monkeypatch.setattr(app_state, "web_settings", {}, raising=False)
    ping = mocker.patch.object(Navidrome, "ping")
    app = FastAPI()
    app.include_router(radio.router)
    async with app.router.lifespan_context(app):
        monitor = app_state.radio_monitor
        assert monitor.navidrome().url == "https://music.example"
        assert (directory / "radio.sqlite3").is_file()
        with monitor.db:
            monitor.db.execute("UPDATE stations SET enabled=1 WHERE id='willy'")
    resumed = RadioMonitor(
        directory / "radio.sqlite3", DownloaderOptions(**DOWNLOADER_OPTIONS)
    )
    try:
        assert resumed.view()["selected_stations"] == ["willy"]
        assert resumed.navidrome().username == "user"
    finally:
        resumed.db.close()
    ping.assert_not_called()
