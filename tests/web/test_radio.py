import logging
from urllib.parse import urlencode

import pytest
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
    assert html.count('name="stations"') == 11
    assert 'value="willy"' in html
    assert 'value="willy-classic"' in html
    assert "https://api.willy.radio/" in html
    assert 'href="/radio"' in html
    assert "https://music.example" in html
    assert "test-password" not in html
    assert "30 minutes" in html


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
    assert monitor.force_poll
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
    with monitor.db:
        monitor.db.execute(
            "UPDATE playlist_state SET error=? WHERE id=1",
            ("<script>playlist error</script>",),
        )
    response = await radio.updates()
    stream = response.body_iterator
    try:
        output = await anext(stream)
    finally:
        await stream.aclose()
    assert "&lt;script&gt;not executable&lt;/script&gt;" in output
    assert "&lt;script&gt;playlist error&lt;/script&gt;" in output
    assert "Nightly Navidrome playlists" in output


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
