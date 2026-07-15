from __future__ import annotations

import asyncio
import threading
import time

import pytest
from sqlalchemy.exc import OperationalError

from app.services import usage_writer


def test_transient_retry_exhaustion_does_not_poison_split(monkeypatch):
    calls: list[list[dict]] = []
    batch = [{"user_id": 1}, {"user_id": 2}]

    def fail_transient(rows):
        calls.append(rows)
        raise OperationalError("insert", {}, RuntimeError("connection lost"))

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(usage_writer, "_write_batch", fail_transient)
    monkeypatch.setattr(usage_writer.asyncio, "sleep", no_sleep)

    with pytest.raises(OperationalError):
        asyncio.run(usage_writer._flush_batch(batch))

    assert calls == [batch, batch, batch]


def test_flush_batch_does_not_block_event_loop_on_sync_db_write(monkeypatch):
    """A lock-waiting usage commit must leave the ASGI loop runnable.

    The production failure was a synchronous ``Session.commit`` on the
    usage-writer task.  A timer releases the fake lock only as a watchdog: if
    the implementation regresses to a direct call, the event loop cannot
    execute the probe until the watchdog fires and the elapsed assertion
    fails instead of hanging the test indefinitely.
    """
    started = threading.Event()
    release = threading.Event()

    def blocked_write(_rows):
        started.set()
        assert release.wait(timeout=2), "test watchdog failed to release fake lock"

    monkeypatch.setattr(usage_writer, "_write_batch", blocked_write)

    async def exercise() -> float:
        probe_started = time.perf_counter()
        task = asyncio.create_task(usage_writer._flush_batch([{"user_id": 1}]))
        # This checkpoint must run while the fake DB write is still blocked.
        await asyncio.sleep(0)
        elapsed = time.perf_counter() - probe_started
        assert await asyncio.to_thread(started.wait, 1)
        release.set()
        await task
        return elapsed

    watchdog = threading.Timer(0.4, release.set)
    watchdog.start()
    try:
        elapsed = asyncio.run(exercise())
    finally:
        release.set()
        watchdog.cancel()

    assert elapsed < 0.2, f"event loop stalled for {elapsed:.3f}s during DB write"
