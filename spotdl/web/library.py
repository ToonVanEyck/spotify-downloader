"""Navidrome library search and recording replacement."""

import asyncio
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from spotdl.download.downloader import Downloader
from spotdl.providers.audio import YouTube, YouTubeMusic
from spotdl.types.result import Result
from spotdl.types.song import Song
from spotdl.utils.ffmpeg import FFMPEG_FORMATS
from spotdl.utils.metadata import get_file_metadata
from spotdl.utils.navidrome import Navidrome
from spotdl.utils.radio import RadioMonitor
from spotdl.utils.web import app_state, get_web_download_output
from spotdl.web.routes import templates

router = APIRouter()
YOUTUBE_HOSTS = {"music.youtube.com", "www.youtube.com", "youtube.com", "youtu.be"}
VIDEO_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{11}")
MAX_RESULTS_PER_PROVIDER = 10


def _monitor() -> RadioMonitor:
    monitor = app_state.radio_monitor
    if monitor is None:
        raise HTTPException(503, "Radio monitoring is not running.")
    return monitor


def _check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "Library changes must be submitted from this app.")


def _youtube_video_id(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in YOUTUBE_HOSTS
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        return None
    try:
        if parsed.port not in (None, 443):
            return None
    except ValueError:
        return None
    if parsed.hostname == "youtu.be":
        video_id = parsed.path.strip("/")
    elif parsed.path == "/watch":
        video_id = parse_qs(parsed.query).get("v", [""])[0]
    else:
        return None
    return video_id if VIDEO_ID_PATTERN.fullmatch(video_id) else None


def _music_root() -> Path:
    configured_root = os.environ.get("NAVIDROME_MUSIC_DIR", "").strip()
    if configured_root:
        root = Path(configured_root).expanduser()
    else:
        web_settings = getattr(app_state, "web_settings", {})
        use_output_dir = web_settings.get("web_use_output_dir", False)
        destination = Path(
            get_web_download_output(app_state.downloader_settings, use_output_dir)
        ).expanduser()
        if not use_output_dir:
            root = destination
        elif destination.is_absolute():
            parts = destination.parts
            first_template_part = next(
                (index for index, part in enumerate(parts) if "{" in part),
                len(parts),
            )
            root = Path(*parts[:first_template_part])
        else:
            root = Path.cwd()
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError(
            "The configured spotDL download location must be an existing directory."
        )
    return root


def _library_file(navidrome_path: str) -> Path:
    root = _music_root()
    path = Path(navidrome_path)
    target = (path if path.is_absolute() else root / path).resolve(strict=True)
    if not target.is_relative_to(root) or not target.is_file():
        raise ValueError("The selected Navidrome track is outside the music directory.")
    return target


def _source_candidates(artist: str, title: str, current_source: object) -> list[Result]:
    query = f"{artist} {title}"
    settings = app_state.downloader_settings
    provider_args: dict[str, Any] = {
        "output_format": settings["format"],
        "cookie_file": settings["cookie_file"],
        "search_query": settings["search_query"],
        "filter_results": settings["filter_results"],
        "yt_dlp_args": settings["yt_dlp_args"],
    }
    providers = (YouTubeMusic(**provider_args), YouTube(**provider_args))
    current_video_id = _youtube_video_id(current_source or "")
    seen: set[str] = set()
    results = []
    for provider in providers:
        provider_result_count = 0
        for options in provider.GET_RESULTS_OPTS:
            for result in provider.get_results(query, **options):
                video_id = _youtube_video_id(result.url)
                if video_id is None or video_id == current_video_id or video_id in seen:
                    continue
                seen.add(video_id)
                results.append(result)
                provider_result_count += 1
                if provider_result_count >= MAX_RESULTS_PER_PROVIDER:
                    break
            if provider_result_count >= MAX_RESULTS_PER_PROVIDER:
                break
    return results


def _song_for_replacement(
    track: dict[str, Any], metadata: dict[str, Any], source_url: str
) -> Song:
    artist = metadata.get("artist") or track["artist"]
    artists = metadata.get("artists") or [artist]
    if isinstance(artists, str):
        artists = [artists]
    if isinstance(artist, list):
        artist = artist[0] if artist else track["artist"]
    return Song.from_missing_data(
        name=metadata.get("name") or track["title"],
        artists=artists,
        artist=artist,
        genres=metadata.get("genres") or [],
        disc_number=metadata.get("disc_number") or 1,
        disc_count=metadata.get("disc_count") or 1,
        album_name=metadata.get("album_name") or track.get("album") or "",
        album_artist=metadata.get("album_artist") or artist,
        duration=metadata.get("duration") or track.get("duration") or 0,
        year=metadata.get("year"),
        date=metadata.get("date"),
        track_number=metadata.get("track_number") or 1,
        tracks_count=metadata.get("tracks_count") or 1,
        song_id=metadata.get("song_id") or track["id"],
        explicit=metadata.get("explicit") or False,
        publisher=metadata.get("publisher") or "",
        url=metadata.get("url") or "",
        isrc=metadata.get("isrc"),
        cover_url=metadata.get("cover_url"),
        copyright_text=metadata.get("copyright_text"),
        lyrics=metadata.get("lyrics"),
        popularity=metadata.get("popularity"),
        download_url=source_url,
    )


async def _replace_file(navidrome: Navidrome, song_id: str, source_url: str) -> None:
    await app_state.download_queue.run(
        lambda: _replace_file_now(navidrome, song_id, source_url)
    )


async def _replace_file_now(
    navidrome: Navidrome, song_id: str, source_url: str
) -> None:
    if _youtube_video_id(source_url) is None:
        raise ValueError("Choose a valid YouTube or YouTube Music suggestion.")
    track = await asyncio.to_thread(navidrome.get_song, song_id)
    target = await asyncio.to_thread(_library_file, track["path"])
    output_format = target.suffix.lower().lstrip(".")
    if output_format not in FFMPEG_FORMATS:
        raise ValueError(
            f"Replacing {output_format or 'unknown'} files is unsupported."
        )
    metadata = await asyncio.to_thread(get_file_metadata, target)
    if metadata is None:
        raise ValueError("Could not read metadata from the selected library file.")
    source = metadata.get("download_url")
    if isinstance(source, list):
        source = source[0] if source else None
    if _youtube_video_id(source or "") == _youtube_video_id(source_url):
        raise ValueError("The selected source is already used by this recording.")
    song = _song_for_replacement(track, metadata, source_url)
    original_mode = stat.S_IMODE(target.stat().st_mode)

    with tempfile.TemporaryDirectory(
        prefix=".spotdl-replacement-", dir=target.parent
    ) as temporary_directory:
        settings = app_state.downloader_settings.copy()
        settings["output"] = str(Path(temporary_directory) / "replacement.{output-ext}")
        settings["format"] = output_format
        settings["overwrite"] = "force"
        settings["scan_for_songs"] = False
        settings["fetch_albums"] = False
        settings["save_file"] = None
        downloader = await asyncio.to_thread(
            Downloader, settings=settings, loop=asyncio.get_running_loop()
        )
        _, replacement = await downloader.pool_download(song)
        if (
            replacement is None
            or not replacement.is_file()
            or replacement.stat().st_size == 0
        ):
            raise RuntimeError("spotDL did not produce a replacement audio file.")
        if replacement.suffix.lower() != target.suffix.lower():
            raise RuntimeError("Replacement format did not match the existing file.")
        replacement.chmod(original_mode)
        replacement.replace(target)


@router.get("/library")
async def library_page(
    request: Request, q: str = "", song_id: str = "", replaced: bool = False
):
    query = q.strip()
    results = []
    selected_song = None
    candidates = []
    error = None
    monitor = _monitor()
    try:
        navidrome = monitor.navidrome()
        if query:
            if len(query) > 200:
                raise ValueError("Search text must be 200 characters or fewer.")
            results = await asyncio.to_thread(navidrome.search_songs, query)
        if song_id:
            track = await asyncio.to_thread(navidrome.get_song, song_id)
            selected_song = {
                "id": track["id"],
                "title": track.get("title", ""),
                "artist": track.get("artist", ""),
                "album": track.get("album", ""),
            }
            target = await asyncio.to_thread(_library_file, track["path"])
            metadata = await asyncio.to_thread(get_file_metadata, target)
            if metadata is None:
                raise ValueError(
                    "Could not read metadata from the selected library file."
                )
            current_source = metadata.get("download_url")
            if isinstance(current_source, list):
                current_source = current_source[0] if current_source else None
            candidates = await asyncio.wait_for(
                asyncio.to_thread(
                    _source_candidates,
                    selected_song["artist"],
                    selected_song["title"],
                    current_source,
                ),
                timeout=30,
            )
    except Exception as exception:  # pylint: disable=broad-exception-caught
        app_state.logger.exception("Navidrome library search failed")
        error = str(exception)

    return templates.TemplateResponse(
        request=request,
        name="library.html.j2",
        context={
            "query": query,
            "results": results,
            "selected_song": selected_song,
            "candidates": candidates,
            "error": error,
            "replaced": replaced,
        },
    )


@router.post("/library/replace")
async def replace_recording(
    request: Request, song_id: str = Form(), source_url: str = Form()
):
    _check_origin(request)
    try:
        await _replace_file(_monitor().navidrome(), song_id, source_url)
    except HTTPException:
        raise
    except Exception as exception:  # pylint: disable=broad-exception-caught
        app_state.logger.exception("Navidrome recording replacement failed")
        return templates.TemplateResponse(
            request=request,
            name="library.html.j2",
            context={
                "query": "",
                "results": [],
                "selected_song": None,
                "candidates": [],
                "error": f"Replacement failed: {exception}",
                "replaced": False,
            },
            status_code=400,
        )
    return RedirectResponse("/library?replaced=true", status_code=303)
