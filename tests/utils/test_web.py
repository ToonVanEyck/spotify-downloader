import asyncio
from collections import deque
from threading import Event, Lock

import pytest

from spotdl.utils.web import Client, DownloadQueue, validate_search_term


def test_web_searchterms():
    url = "https://open.spotify.com/intl-pt/track/example"
    result = validate_search_term(url)
    assert result is True


def test_session_output_is_bounded():
    client = Client.__new__(Client)
    client.log_lines = deque(maxlen=1000)
    client.log_lock = Lock()

    client.append_log("\n".join(str(line) for line in range(1100)))

    assert client.get_log_output().splitlines() == [
        str(line) for line in range(100, 1100)
    ]

    client.append_log("x" * 9000)
    assert len(client.get_log_output().splitlines()[-1]) == 8000


async def test_download_queue_releases_slot_after_failure():
    queue = DownloadQueue()

    async def fail():
        raise ValueError("download failed")

    async def succeed():
        return "finished"

    with pytest.raises(ValueError, match="download failed"):
        await queue.run(fail)
    assert await queue.run(succeed) == "finished"


async def test_cancelled_download_holds_slot_until_worker_finishes():
    queue = DownloadQueue()
    started = Event()
    release = Event()
    next_started = asyncio.Event()

    def worker():
        started.set()
        assert release.wait(timeout=5)

    async def download():
        await asyncio.to_thread(worker)

    async def next_download():
        next_started.set()

    active = asyncio.create_task(queue.run(download))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        active.cancel()
        waiting = asyncio.create_task(queue.run(next_download))
        await asyncio.sleep(0)
        active.cancel()
        await asyncio.sleep(0)

        assert not active.done()
        assert not next_started.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await active
    await asyncio.wait_for(waiting, timeout=1)
    assert next_started.is_set()


async def test_cancelled_waiting_download_is_not_started():
    queue = DownloadQueue()
    started = asyncio.Event()
    release = asyncio.Event()

    async def download():
        started.set()
        await release.wait()

    async def cancelled_download():
        pytest.fail("A cancelled queued download must not run")

    active = asyncio.create_task(queue.run(download))
    await started.wait()
    waiting = asyncio.create_task(queue.run(cancelled_download))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    release.set()
    await active
