"""Radio monitoring page, configuration forms, and application lifecycle."""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from datastar_py.fastapi import ServerSentEventGenerator as SSE
from fastapi import APIRouter, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from spotdl.utils.config import get_spotdl_path
from spotdl.utils.navidrome import NavidromeError
from spotdl.utils.radio import RadioMonitor
from spotdl.utils.web import app_state
from spotdl.web.routes import templates
from spotdl.web.utils import datastar_response


@asynccontextmanager
async def monitor_lifespan(_app: FastAPI):
    """Own exactly one monitor for the server, not one per connected browser."""

    config_directory = os.environ.get("SPOTDL_RADIO_CONFIG_DIR")
    monitor = RadioMonitor(
        (Path(config_directory) if config_directory else get_spotdl_path())
        / "radio.sqlite3",
        app_state.downloader_settings,
        app_state.web_settings.get("web_use_output_dir", False),
    )
    app_state.radio_monitor = monitor
    try:
        await monitor.start()
        yield
    finally:
        await monitor.stop()
        app_state.radio_monitor = None


router = APIRouter(lifespan=monitor_lifespan)


def get_monitor() -> RadioMonitor:
    """Fail explicitly if monitoring has not been started by the application."""

    if app_state.radio_monitor is None:
        raise HTTPException(503, "Radio monitoring is not running.")
    return app_state.radio_monitor


def check_origin(request: Request) -> None:
    """Do not allow another website to submit credentials or change monitoring."""

    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "Radio settings must be changed from this app.")


@router.get("/radio")
async def radio_page(request: Request):
    """Show public settings without returning stored credentials to the browser."""

    return templates.TemplateResponse(
        request=request,
        name="radio.html.j2",
        context=get_monitor().view(),
    )


@router.post("/radio/settings")
async def save_settings(
    request: Request,
    stations: Optional[list[str]] = Form(default=None),
    navidrome_url: str = Form(default=""),
    navidrome_username: str = Form(default=""),
    navidrome_password: str = Form(default=""),
):
    """Save selected stations only after the Navidrome connection is validated."""

    check_origin(request)
    monitor = get_monitor()
    try:
        await monitor.configure(
            stations or [], navidrome_url, navidrome_username, navidrome_password
        )
    except (ValueError, NavidromeError) as exception:
        app_state.logger.warning("Radio settings not saved: %s", exception)
        monitor.append_log(f"ERROR Settings not saved: {exception}")
        context = monitor.view()
        context["error"] = str(exception)
        context["selected_stations"] = stations or []
        context["navidrome_url"] = navidrome_url.strip()
        context["navidrome_username"] = navidrome_username.strip()
        return templates.TemplateResponse(
            request=request, name="radio.html.j2", context=context, status_code=400
        )
    return RedirectResponse("/radio?saved=1", status_code=303)


@router.post("/radio/check")
async def check_now(request: Request):
    """Wake the poller without bypassing per-station refill rate limits."""

    check_origin(request)
    get_monitor().check_now()
    return RedirectResponse("/radio?checking=1", status_code=303)


@router.get("/radio/updates")
@datastar_response
async def updates():
    """Stream monitoring progress without sending credentials as client signals."""

    monitor = get_monitor()
    previous = None
    previous_stations = None
    while True:
        context = monitor.view()
        rendered = templates.get_template("radio-status.html.j2").render(**context)
        if rendered != previous:
            yield SSE.patch_elements(rendered)
            previous = rendered
        if context["selected_stations"] != previous_stations:
            # A scalar avoids transient partial array updates resetting the filter.
            yield SSE.patch_signals(
                {
                    "radio_enabled_stations": "|"
                    + "|".join(context["selected_stations"])
                    + "|"
                }
            )
            previous_stations = context["selected_stations"]
        await asyncio.sleep(2)
