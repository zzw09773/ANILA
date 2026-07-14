"""Minimal Gate 4 wire harness: agent SSE -> CSP validator -> terminal.

This deliberately avoids a live container/model.  The fake agent emits the
same named SSE frames as the official and LangChain fixtures; the assertions
exercise the CSP trust boundary and in-session cancellation race directly.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from anila_contracts import Classification, StepEvent
from app.services.proxy.cancellation import (
    InSessionCancellationRegistry,
    StreamCancelled,
    cancellable_iter,
)
from app.services.proxy.stream_bridge import (
    BridgeContext,
    StreamValidator,
    cancelled_terminal_frame,
)


def _context() -> BridgeContext:
    return BridgeContext(
        task_id="trusted-task",
        trace_id="trusted-trace",
        agent_id="trusted-agent",
        session_id="trusted-session",
        run_id="trusted-run",
        classification=Classification.CONFIDENTIAL,
    )


def _step(**updates) -> dict:
    value = StepEvent(
        event_id="agent-event",
        sequence=1,
        cursor="1",
        trace_id="forged-trace",
        task_id="forged-task",
        session_id="forged-session",
        invocation_id="agent-invocation",
        run_id="forged-run",
        step_id="tool:1",
        kind="tool",
        status="running",
        agent_id="forged-agent",
        tool_name="search_documents",
        safe_input_summary="開始文件搜尋",
        classification=Classification.UNCLASSIFIED,
    ).model_dump(mode="json")
    value.update(updates)
    return value


def _payload(frame: str) -> dict:
    return json.loads(
        next(line[6:] for line in frame.splitlines() if line.startswith("data: "))
    )


def test_fake_agent_named_sse_is_validated_before_shell_delivery(caplog):
    validator = StreamValidator(_context(), max_events_per_second=8, max_events_per_run=8)
    fake_agent_frames = [
        ("anila.step", json.dumps(_step())),
        ("anila.reasoning", json.dumps({"delta": "raw reasoning"})),
        ("anila.future", "{}"),
        ("anila.step", "not-json"),
        ("anila.step", json.dumps(_step(safe_output_summary="api_key=sk-abcdefghijklmnopqrst"))),
    ]
    delivered = [
        output
        for event_name, data in fake_agent_frames
        if (output := validator.validate(event_name, data)) is not None
    ]

    assert len(delivered) == 1
    payload = _payload(delivered[0])
    assert payload["task_id"] == "trusted-task"
    assert payload["trace_id"] == "trusted-trace"
    assert payload["agent_id"] == "trusted-agent"
    assert payload["session_id"] == "trusted-session"
    assert payload["run_id"] == "trusted-run"
    assert payload["classification"] == "機密"
    assert "raw reasoning" not in "".join(delivered)
    assert "sk-abcdefghijklmnopqrst" not in "".join(delivered)
    assert "stream_event_dropped" in caplog.text


async def _neverending_agent():
    yield "agent-frame"
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_cancel_harness_emits_one_csp_authored_cancelled_terminal():
    registry = InSessionCancellationRegistry()
    terminals: list[str] = []
    async with registry.register(77) as cancel_event:
        stream = cancellable_iter(_neverending_agent(), cancel_event)
        assert await anext(stream) == "agent-frame"
        assert (await registry.cancel(77)).accepted is True
        with pytest.raises(StreamCancelled):
            await anext(stream)
        if await registry.claim_cancel_terminal(77, cancel_event):
            terminals.append(cancelled_terminal_frame(_context()))
        await stream.aclose()

    assert len(terminals) == 1
    terminal = StepEvent.model_validate(_payload(terminals[0]))
    assert terminal.status.value == "cancelled"
    assert terminal.task_id == "trusted-task"
    assert terminal.classification.value == "機密"
