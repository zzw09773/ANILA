"""Backpressure + disconnect-cancellation for the agent SSE path (Q23).

All awaits are wrapped in ``asyncio.wait_for`` so a regression fails fast
instead of hanging the suite (the failure mode of the Q23 one-liner).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from anila_core.api.server import (
    STREAM_EVENT_QUEUE_MAXSIZE,
    _stream_engine_run,
    create_app,
)
from anila_core.models.message import StreamDelta, Usage
from anila_core.providers.mock import MockProvider, ScriptedResponse
from anila_core.router.tool_router import ToolRegistry

TIMEOUT = 2.0


def _parse_sse(body: str) -> list[dict]:
    out: list[dict] = []
    for block in body.strip().split("\n\n"):
        event_name = ""
        data_payload = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                event_name = line[7:]
            elif line.startswith("data: "):
                data_payload = line[6:]
        if data_payload:
            try:
                parsed = json.loads(data_payload)
            except json.JSONDecodeError:
                continue
            out.append({"event": event_name, "data": parsed})
    return out


def _text_delta(text: str = "x") -> StreamDelta:
    return StreamDelta(type="text", text=text)


class _QueueProbe:
    """Capture the stream queue instance and its high-water qsize."""

    def __init__(self) -> None:
        self.queue: Optional[asyncio.Queue[Any]] = None
        self.max_qsize = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        probe = self
        real_queue = asyncio.Queue

        class TrackingQueue(real_queue):  # type: ignore[valid-type,misc]
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                probe.queue = self

            async def put(self, item: Any) -> None:  # type: ignore[override]
                await super().put(item)
                probe.max_qsize = max(probe.max_qsize, self.qsize())

            def put_nowait(self, item: Any) -> None:  # type: ignore[override]
                super().put_nowait(item)
                probe.max_qsize = max(probe.max_qsize, self.qsize())

        monkeypatch.setattr(asyncio, "Queue", TrackingQueue)


@pytest.mark.asyncio
async def test_bounded_queue_suspends_producer_when_consumer_stalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a) Stalled consumer → producer suspends; queue never exceeds bound."""
    bound = STREAM_EVENT_QUEUE_MAXSIZE
    total = bound + 50
    emitted = 0
    probe = _QueueProbe()
    probe.install(monkeypatch)

    async def coro_factory(on_delta: Any) -> None:
        nonlocal emitted
        for i in range(total):
            await on_delta(_text_delta(f"t{i}"))
            emitted += 1

    engine = SimpleNamespace(_session=None)
    agen = _stream_engine_run(
        session_id="s-bp-a",
        engine=engine,  # type: ignore[arg-type]
        coro_factory=coro_factory,
    )

    # Consume a few events, then stall (stop calling __anext__).
    k = 3
    for _ in range(k):
        await asyncio.wait_for(agen.__anext__(), timeout=TIMEOUT)

    # Let the producer fill the bounded buffer and block on put.
    await asyncio.sleep(0.05)

    assert probe.queue is not None
    assert probe.queue.maxsize == bound
    assert probe.queue.qsize() <= bound
    assert probe.max_qsize <= bound
    # Producer has not finished: backpressure suspended further puts.
    assert emitted < total
    assert emitted <= k + bound

    await asyncio.wait_for(agen.aclose(), timeout=TIMEOUT)


@pytest.mark.asyncio
async def test_disconnect_cancels_engine_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) Closing the consumer cancels the upstream engine task."""
    tasks: list[asyncio.Task[Any]] = []
    real_create_task = asyncio.create_task

    def capture_task(coro: Any, **kwargs: Any) -> asyncio.Task[Any]:
        task = real_create_task(coro, **kwargs)
        tasks.append(task)
        return task

    monkeypatch.setattr(asyncio, "create_task", capture_task)

    engine_cancelled = asyncio.Event()

    async def coro_factory(on_delta: Any) -> None:
        try:
            while True:
                await on_delta(_text_delta("tick"))
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            engine_cancelled.set()
            raise

    engine = SimpleNamespace(_session=None)
    agen = _stream_engine_run(
        session_id="s-bp-b",
        engine=engine,  # type: ignore[arg-type]
        coro_factory=coro_factory,
    )

    await asyncio.wait_for(agen.__anext__(), timeout=TIMEOUT)
    await asyncio.wait_for(agen.aclose(), timeout=TIMEOUT)

    await asyncio.wait_for(engine_cancelled.wait(), timeout=TIMEOUT)
    assert tasks, "expected run_engine task to be captured"
    engine_task = tasks[0]
    assert engine_task.done()
    # cancelled() is the common path; swallowing CancelledError only around
    # the sentinel put can leave the task "done" without cancelled()—the
    # engine_cancelled event still proves cancellation entered the coro.
    assert engine_task.cancelled() or engine_cancelled.is_set()


@pytest.mark.asyncio
async def test_cancel_while_queue_full_does_not_deadlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(c) Full queue + consumer cancel settles within timeout (no deadlock)."""
    bound = STREAM_EVENT_QUEUE_MAXSIZE
    emitted = 0
    producer_finished = asyncio.Event()
    tasks: list[asyncio.Task[Any]] = []
    real_create_task = asyncio.create_task

    def capture_task(coro: Any, **kwargs: Any) -> asyncio.Task[Any]:
        task = real_create_task(coro, **kwargs)
        tasks.append(task)
        return task

    monkeypatch.setattr(asyncio, "create_task", capture_task)

    async def coro_factory(on_delta: Any) -> None:
        nonlocal emitted
        try:
            # Emit far more than the bound so we block on put once full.
            for i in range(bound + 100):
                await on_delta(_text_delta(f"f{i}"))
                emitted += 1
        finally:
            producer_finished.set()

    engine = SimpleNamespace(_session=None)
    agen = _stream_engine_run(
        session_id="s-bp-c",
        engine=engine,  # type: ignore[arg-type]
        coro_factory=coro_factory,
    )

    await asyncio.wait_for(agen.__anext__(), timeout=TIMEOUT)

    async def _queue_full() -> None:
        # After 1 consume, buffer is full once emitted == bound + 1; the next
        # put blocks without incrementing emitted.
        while emitted < bound + 1:
            await asyncio.sleep(0.001)

    await asyncio.wait_for(_queue_full(), timeout=TIMEOUT)
    # Producer should be wedged on put (not finished) with a full buffer.
    assert emitted == bound + 1
    assert not producer_finished.is_set()
    # Brief yield so the blocked put is scheduled.
    await asyncio.sleep(0.02)
    assert not producer_finished.is_set()
    assert emitted == bound + 1

    # The Q23 failure mode: cancel while full must not hang.
    await asyncio.wait_for(agen.aclose(), timeout=TIMEOUT)
    await asyncio.wait_for(producer_finished.wait(), timeout=TIMEOUT)
    assert tasks[0].done()


@pytest.mark.asyncio
async def test_normal_completion_emits_usage_then_stream_done() -> None:
    """(d) Sentinel path unchanged: USAGE_UPDATE then STREAM_DONE."""
    usage = Usage(input_tokens=11, output_tokens=22)

    async def coro_factory(on_delta: Any) -> None:
        await on_delta(_text_delta("hello"))
        await on_delta(StreamDelta(type="stop", usage=usage, finish_reason="end_turn"))

    engine = SimpleNamespace(_session=None)
    agen = _stream_engine_run(
        session_id="s-bp-d",
        engine=engine,  # type: ignore[arg-type]
        coro_factory=coro_factory,
    )

    chunks: list[str] = []
    while True:
        try:
            chunks.append(await asyncio.wait_for(agen.__anext__(), timeout=TIMEOUT))
        except StopAsyncIteration:
            break

    events = _parse_sse("".join(chunks))
    types = [e["event"] for e in events]
    assert "message_delta" in types
    assert "usage_update" in types
    assert "stream_done" in types
    assert types.index("usage_update") < types.index("stream_done")
    assert types[-1] == "stream_done"
    assert events[-1]["data"]["payload"]["status"] == "completed"
    usage_event = next(e for e in events if e["event"] == "usage_update")
    assert usage_event["data"]["payload"]["input_tokens"] == 11
    assert usage_event["data"]["payload"]["output_tokens"] == 22
    assert "error" not in types


def test_http_chat_stream_still_completes_without_error_event() -> None:
    """Stop-button contract via HTTP: normal stream has no error event."""
    provider = MockProvider(
        [ScriptedResponse(text="hi there", finish_reason="end_turn")]
    )
    app = create_app(
        provider=provider,
        tool_registry=ToolRegistry(),
        api_dev_mode=True,
        session_factory=lambda sid: __import__(
            "anila_core.memory", fromlist=["MemorySession"]
        ).MemorySession(sid),
    )
    client = TestClient(app)
    response = client.post(
        "/chat",
        json={
            "session_id": "s-bp-http",
            "user_message": "hi",
            "history": [],
            "system_prompt": "x",
        },
    )
    assert response.status_code == 200
    events = _parse_sse(response.text)
    types = [e["event"] for e in events]
    assert "usage_update" in types
    assert types[-1] == "stream_done"
    assert "error" not in types
