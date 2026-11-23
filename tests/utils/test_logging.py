import asyncio
import logging
from logging import LogRecord

import pytest

from spotdl.providers.audio.base import AudioProviderError, YTDLLogger
from spotdl.utils.logging import (
    DEBUG,
    NOTSET,
    SpotdlFormatter,
    capture_web_output,
    web_log_output,
)


def test_spotdl_formatter_format():
    # cf. https://rich.readthedocs.io/en/stable/markup.html#escaping
    formatter = SpotdlFormatter()

    input_output_map = {
        ("[as it is, infinite]", DEBUG): "[blue]\\[as it is, infinite]",
        ("[effluvium]", NOTSET): "\\[effluvium]",
        ("DRIP", DEBUG): "[blue]DRIP",
        ("FOREIGN TONGUES", NOTSET): "FOREIGN TONGUES",
    }

    for (msg, level), escaped_msg in input_output_map.items():
        assert (
            formatter.format(
                LogRecord("spotdl", level, "", 0, msg, None, None, None, None)
            )
            == escaped_msg
        )


async def test_web_output_is_isolated_across_tasks_and_workers():
    first, second = [], []
    spotdl_logger = logging.getLogger("spotdl.test-output")
    handlers_before = list(logging.getLogger("spotdl").handlers)

    async def download(name, output):
        with capture_web_output(output.append):
            await asyncio.sleep(0)
            await asyncio.to_thread(spotdl_logger.error, f"{name} download failed")
            await asyncio.to_thread(YTDLLogger().debug, f"{name} extracting")
            await asyncio.to_thread(YTDLLogger().warning, f"{name} retrying")
            with pytest.raises(AudioProviderError):
                await asyncio.to_thread(YTDLLogger().error, f"{name} unavailable")

    await asyncio.gather(download("first", first), download("second", second))
    YTDLLogger().warning("not in a session")
    spotdl_logger.error("not in a session")

    for name, output in [("first", first), ("second", second)]:
        assert output == [
            f"ERROR {name} download failed",
            f"[yt-dlp] {name} extracting",
            f"WARNING [yt-dlp] {name} retrying",
            f"ERROR [yt-dlp] {name} unavailable",
        ]
    assert web_log_output.get() is None
    assert logging.getLogger("spotdl").handlers == handlers_before


def test_web_output_handler_is_removed_after_error():
    handlers_before = list(logging.getLogger("spotdl").handlers)
    with pytest.raises(ValueError), capture_web_output(lambda message: None):
        raise ValueError("failed")

    assert web_log_output.get() is None
    assert logging.getLogger("spotdl").handlers == handlers_before
