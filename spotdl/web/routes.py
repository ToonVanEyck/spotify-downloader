"""
Module which contains the web client routes and functions.
"""

import asyncio
import uuid
from pathlib import Path
from typing import Any, Optional, cast

# from datastar_py.sse import DatastarEvent
from datastar_py.fastapi import ReadSignals
from datastar_py.fastapi import (
    ServerSentEventGenerator as SSE,  # DatastarResponse,; read_signals,
)
from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from spotdl._version import __version__
from spotdl.download.downloader import AUDIO_PROVIDERS, LYRICS_PROVIDERS
from spotdl.utils.config import get_spotdl_path
from spotdl.utils.ffmpeg import FFMPEG_FORMATS
from spotdl.utils.search import (
    get_search_suggestions,
    get_simple_songs,
    get_spotify_url_preview,
)
from spotdl.utils.web import Client, app_state, validate_search_term
from spotdl.web.utils import Signals, datastar_response, handle_signals

__all__ = ["router"]

router = APIRouter()

templates = Jinja2Templates(directory=Path(__file__).parent / "components")


# PATHS


@router.get("/")
async def home(request: Request):
    """
    Handle the home page request.
    """
    return templates.TemplateResponse(
        request=request,
        name="home.html.j2",
        context={"__version__": __version__},
    )


@router.get("/search")
async def search(q: Optional[str], request: Request):
    """
    Handle the search input.
    """
    return templates.TemplateResponse(
        request=request,
        name="home.html.j2",
        context={"__version__": __version__, "search_term": q},
    )


@router.get("/downloads")
async def downloads(request: Request):
    """
    Handle the downloads page request.
    """
    return templates.TemplateResponse(
        request=request,
        name="home.html.j2",
        context={"__version__": __version__, "search_term": ""},
    )


# ACTIONS


@router.get("/client/load")
@datastar_response
async def handle_get_client_load(datastar_signals: ReadSignals):
    """
    Handle the loading of the client.
    """
    app_state.logger.info("Loading client...")
    signals = handle_signals(datastar_signals)
    if not signals.client_id:
        # Generate a new client ID if not provided
        app_state.logger.warning("No client ID provided, generating a new one.")
        signals.client_id = uuid.uuid4().hex
        client = Client(signals.client_id)
    else:
        found_client = Client.get_instance(signals.client_id)
        if found_client is None:
            # Create a new client if not found
            app_state.logger.warning(
                f"Client {signals.client_id} not found, creating new client..."
            )
            signals.client_id = uuid.uuid4().hex
            client = Client(signals.client_id)
        else:
            client = found_client
    await client.connect()

    # First send the client ID and then the home template.
    yield SSE.patch_elements("""<div id="status"></div>""")
    # Send the client ID to the client
    yield SSE.patch_signals(
        {
            "client_id": client.client_id,
        }
    )
    try:
        while True:
            yield SSE.patch_elements(f"""<div id="overall-completed-tasks">
                {len(client.downloader.progress_handler.progress_tracker.songs)}
                </div>""")
            await asyncio.sleep(1)
    finally:
        app_state.logger.info(f"[{signals.client_id}] Unloading client...")
        await client.disconnect()


@router.get("/client/search")
@datastar_response
async def handle_get_client_search(datastar_signals: ReadSignals):
    """
    Handle the search input.
    """
    app_state.logger.info("Loading search...")
    signals = handle_signals(datastar_signals)
    app_state.logger.info(f"[{signals.client_id}] Search term: {signals.search_term}")
    is_valid_url = validate_search_term(signals.search_term)

    error = None
    preview = None
    suggestions = {"artists": [], "tracks": [], "albums": []}
    try:
        if is_valid_url:
            preview = await asyncio.wait_for(
                asyncio.to_thread(get_spotify_url_preview, signals.search_term),
                timeout=30,
            )
        else:
            suggestions = await asyncio.wait_for(
                asyncio.to_thread(
                    get_search_suggestions, signals.search_term, limit=10
                ),
                timeout=30,
            )
    except asyncio.TimeoutError:
        app_state.logger.error("Search timed out for %s", signals.search_term)
        error = (
            "Search timed out after 30 seconds. Check your connection and try again."
        )
    except Exception as exception:  # pylint: disable=broad-exception-caught
        app_state.logger.exception("Search failed for %s", signals.search_term)
        error = f"Search failed: {exception}"

    yield SSE.patch_elements(
        templates.get_template("search-list.html.j2").render(
            suggestions=suggestions, preview=preview, error=error
        )
    )


@router.get("/client/downloads")
@datastar_response
async def handle_get_client_downloads(datastar_signals: ReadSignals):
    """
    Handle the retrieval of client downloads.
    """
    app_state.logger.info("Loading downloads...")
    signals = handle_signals(datastar_signals)
    app_state.logger.info(f"[{signals.client_id}] Downloads requested.")
    client = Client.get_instance(signals.client_id)
    if client is None:
        app_state.logger.warning(
            f"[{signals.client_id}] Client not found, cannot load downloads."
        )
        yield SSE.patch_elements(
            templates.get_template("status-disconnected.html.j2").render()
        )
        return
    while True:
        client_song_downloads = (
            client.downloader.progress_handler.progress_tracker.songs
        )
        yield SSE.patch_elements(
            templates.get_template("download-list.html.j2").render(
                client_song_downloads=client_song_downloads.values()
            )
        )
        await asyncio.sleep(1)


@router.get("/client/settings")
@datastar_response
async def handle_get_client_settings(datastar_signals: ReadSignals):
    """
    Handle the retrieval of client settings.
    """
    signals = handle_signals(datastar_signals)
    client = Client.get_instance(signals.client_id)
    if client is None:
        app_state.logger.warning(
            f"Client {signals.client_id} not found, cannot update settings."
        )
        yield SSE.patch_elements(
            templates.get_template("status-disconnected.html.j2").render()
        )
        return
    app_state.logger.info(f"[{signals.client_id}] Sending client settings...")
    yield SSE.patch_signals(
        {
            "downloader_settings": cast(Any, client.downloader_settings),
        }
    )


@router.post("/client/settings")
@datastar_response
async def handle_post_client_settings(datastar_signals: ReadSignals):
    """
    Handle the update of client settings.
    """
    signals = handle_signals(datastar_signals)
    client = Client.get_instance(signals.client_id)
    error = None
    if client is not None and signals.downloader_settings is not None:
        app_state.logger.info(f"[{signals.client_id}] Updating settings...")
        try:
            client.update_settings(signals.downloader_settings)
        except ValueError as exception:
            error = str(exception)
            app_state.logger.warning(
                f"[{signals.client_id}] Invalid settings, not saved: {error}"
            )

    if client is not None and error is None:
        yield SSE.patch_elements("""
                <div id="settings-status">
                    <div id="settings-is-saved" class="alert alert-success shadow-lg">
                        <div>
                            <svg 
                            xmlns="http://www.w3.org/2000/svg" 
                            class="stroke-current flex-shrink-0 h-6 w-6" 
                            fill="none"
                            viewBox="0 0 24 24">
                                <path 
                                stroke-linecap="round" 
                                stroke-linejoin="round" 
                                stroke-width="2"
                                d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                            </svg>
                            <span>Changes saved</span>
                        </div>
                    </div>
                </div>
            """)
        yield SSE.patch_signals(
            {
                "downloader_settings": cast(Any, client.downloader_settings),
            }
        )
    else:
        if client is None:
            app_state.logger.warning(
                f"[{signals.client_id}] Client not found, cannot update settings."
            )
            yield SSE.patch_elements(
                templates.get_template("status-disconnected.html.j2").render()
            )
        else:
            # Put the settings shown in the UI back to the saved ones
            yield SSE.patch_signals(
                {
                    "downloader_settings": cast(Any, client.downloader_settings),
                }
            )
        yield SSE.patch_elements("""
                <div id="settings-status">
                    <div id="settings-is-not-saved" class="alert alert-error shadow-lg">
                        <div>
                            <svg 
                            xmlns="http://www.w3.org/2000/svg" 
                            class="stroke-current 
                            flex-shrink-0 h-6 w-6" 
                            fill="none"
                            viewBox="0 0 24 24">
                                <path 
                                stroke-linecap="round" 
                                stroke-linejoin="round" 
                                stroke-width="2"
                d="M10 14l2-2m0 0l2-2m-2 2l-2-2m2 2l2 2m7-2a9 9 0 11-18 0 9 9 0 0118 0z" 
                            />
                            </svg>
                            <span>Error! Unable to save settings</span>
                        </div>
                    </div>
                </div>
            """)
    #  sleep for 3 seconds then clear the status message
    await asyncio.sleep(3)
    yield SSE.patch_elements("""
            <div id="settings-status">
            </div>
        """)


@router.post("/client/download/")
@datastar_response
async def handle_post_client_download(datastar_signals: ReadSignals):
    """
    Handle the download request from the client.
    """
    signals = handle_signals(datastar_signals)
    async for update in gen_download(signals):
        yield update


# HELPERS


async def gen_download(signals: Signals):
    """
    Generate the download process for the client.
    """
    client = Client.get_instance(signals.client_id)
    if client is None:
        app_state.logger.warning(
            f"Client {signals.client_id} not found, cannot load downloads."
        )
        yield SSE.patch_elements(
            templates.get_template("status-disconnected.html.j2").render()
        )
        return
    app_state.logger.info(
        f"[{signals.client_id}] Download requested: {signals.song_url}"
    )
    yield SSE.patch_elements(
        '<div id="download-status" role="status">Preparing download...</div>'
    )

    # Use the server's output setting, clients can't choose where files go
    if app_state.web_settings.get("web_use_output_dir", False):
        client.downloader.settings["output"] = app_state.downloader_settings["output"]
    else:
        client.downloader.settings["output"] = str(
            (get_spotdl_path() / f"web/sessions/{client.client_id}").absolute()
        )

    try:
        settings = client.downloader.settings
        songs = await asyncio.to_thread(
            get_simple_songs,
            [signals.song_url],
            use_ytm_data=settings["ytm_data"],
            playlist_numbering=settings["playlist_numbering"],
            albums_to_ignore=settings["ignore_albums"],
            album_type=settings["album_type"],
            playlist_retain_track_cover=settings["playlist_retain_track_cover"],
        )
        app_state.logger.info("Downloading %d song(s)", len(songs))

        if not songs:
            app_state.logger.warning("No tracks found for %s", signals.song_url)
            yield SSE.patch_elements(
                '<div id="download-status" role="status">No tracks found for this Spotify URL.</div>'
            )
            return

        yield SSE.patch_elements(
            f'<div id="download-status" role="status">Downloading {len(songs)} track(s)...</div>'
        )

        downloads = [
            asyncio.create_task(client.downloader.pool_download(song)) for song in songs
        ]
        failures = 0
        for completed, download in enumerate(asyncio.as_completed(downloads), start=1):
            try:
                song, path = await download
                if path is None:
                    app_state.logger.error("Failure downloading %s", song.name)
                    failures += 1
            except Exception as exception:  # pylint: disable=broad-exception-caught
                app_state.logger.error("Error downloading track: %s", exception)
                failures += 1

            yield SSE.patch_elements(
                f'<div id="download-status" role="status">Processed {completed} of {len(songs)} tracks...</div>'
            )

        message = (
            f"Finished with {failures} failed track(s)."
            if failures
            else "Download complete."
        )
        yield SSE.patch_elements(
            f'<div id="download-status" role="status">{message}</div>'
        )

        # return str(path.absolute())

    except Exception as exception:
        app_state.logger.error(f"Error downloading! {exception}")
        yield SSE.patch_elements(f"""
            <div id="download-status" class="alert alert-error" role="alert">
                Could not process Spotify URL: {exception}
            </div>
        """)


# COMPONENTS


@router.get("/client/component/settings-content")
@datastar_response
async def handle_get_client_component_settings(datastar_signals: ReadSignals):
    """
    Handle the request for the client settings component.
    """
    signals = handle_signals(datastar_signals)
    client = Client.get_instance(signals.client_id)
    if client is None:
        app_state.logger.warning(
            f"Client {signals.client_id} not found, cannot update settings."
        )
        yield SSE.patch_elements(
            templates.get_template("status-disconnected.html.j2").render()
        )
        return
    app_state.logger.info(f"[{signals.client_id}] Loading settings view...")
    # clear state
    yield SSE.patch_elements("""<div id="component-settings-content"></div>""")
    # render the settings content
    yield SSE.patch_elements(
        templates.get_template("settings-content.html.j2").render(
            downloader_settings=client.downloader_settings,
            AUDIO_PROVIDERS=AUDIO_PROVIDERS,
            LYRICS_PROVIDERS=LYRICS_PROVIDERS,
            FORMATS=FFMPEG_FORMATS.keys(),
        )
    )
    # spotify_client = SpotifyClient()
    # print(f"{spotify_client = }")
    yield SSE.patch_signals(
        {
            "downloader_settings": cast(Any, client.downloader_settings),
            # "spotify_settings": {
            #     "client_id": spotify_client.credential_manager.client_id
            # },
        }
    )


@router.get("/client/component/search-input-rotating-placeholder")
@datastar_response
async def handle_client_component_search_input_rotating_placeholder():
    """
    Handle the search input rotating placeholder component.
    """
    app_state.logger.info("Loading rotating-placeholder...")
    placeholder_items = [
        "All Eyes On Me - Bo Burnham",
        "https://open.spotify.com/track/4vfN00PlILRXy5dcXHQE9M?si=e4d9e7c044dd4a8f",
        "Lil Wayne",
        "Drive - Miley Cyrus",
        "Sofia - TMG",
        "Lightning Crashes - Live",
    ]
    index = 0
    while True:
        t = templates.get_template("search-input-rotating-placeholder.html.j2").render(
            placeholder_item=placeholder_items[index]
        )
        yield SSE.patch_elements(t)
        await asyncio.sleep(5)
        index += 1
        if index >= len(placeholder_items):
            index = 0
