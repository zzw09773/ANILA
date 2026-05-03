"""Tests for Sprint 13 PR A1 — Router SSE event-header parsing + pass-through.

The Router proxies an upstream agent's ``/v1/chat/completions`` stream and
needs to forward both:

  * unnamed ``data:`` chunks (OpenAI envelopes carrying delta text); and
  * named ``event: <name>`` events that the agent template / Sprint 9-12
    QueryEngine endpoints emit (anila.trace, anila.meta, anila.reasoning,
    plus the typed events interrupt_requested / todos_updated /
    follow_ups / tool_call_started / tool_call_finished / …).

The previous implementation only parsed ``data:`` lines and silently
dropped every named SSE event, so anila.meta from agents using the
template format never reached the user-facing stream and Sprint 9-12
typed events were invisible end-to-end. These tests pin the new parser.
"""

from __future__ import annotations

import json
from typing import AsyncIterator

import httpx
import pytest
import respx

from anila_core.api.router_server import _stream_agent_sse
from anila_core.config import settings


CSP_URL = f"{settings.csp_base_url}/v1/chat/completions"


def _sse_response(body: str) -> httpx.Response:
    """Return a Response object that respx can hand back as a streamed body."""
    return httpx.Response(
        200,
        content=body.encode("utf-8"),
        headers={"Content-Type": "text/event-stream"},
    )


async def _collect(agent_id: str, query: str) -> list[dict]:
    events: list[dict] = []
    async for ev in _stream_agent_sse(agent_id, query, "k"):
        events.append(ev)
    return events


# ---------------------------------------------------------------------------
# Backward compat: plain OpenAI delta stream (no named events)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_plain_openai_delta_stream_yields_content() -> None:
    body = (
        'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    respx.post(CSP_URL).mock(return_value=_sse_response(body))
    events = await _collect("a", "q")
    assert [e["type"] for e in events] == ["content", "content", "done"]
    assert "".join(e["content"] for e in events if e["type"] == "content") == "hello"


@pytest.mark.asyncio
@respx.mock
async def test_legacy_anila_meta_field_in_chunk() -> None:
    """Old agents embedded ``anila_meta`` directly in the OpenAI chunk envelope."""
    body = (
        'data: {"anila_meta":{"trace":[{"label":"x"}]},"choices":[{"delta":{}}]}\n\n'
        "data: [DONE]\n\n"
    )
    respx.post(CSP_URL).mock(return_value=_sse_response(body))
    events = await _collect("a", "q")
    metas = [e for e in events if e["type"] == "meta"]
    assert len(metas) == 1
    assert metas[0]["anila_meta"] == {"trace": [{"label": "x"}]}


# ---------------------------------------------------------------------------
# Named anila.* events from the agent template
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_anila_trace_event_passes_through_unchanged() -> None:
    payload = {"kind": "agent", "label": "step", "detail": "d", "status": "ok"}
    body = (
        f"event: anila.trace\ndata: {json.dumps(payload)}\n\n"
        'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    respx.post(CSP_URL).mock(return_value=_sse_response(body))
    events = await _collect("a", "q")

    anila_evs = [e for e in events if e["type"] == "anila_event"]
    assert len(anila_evs) == 1
    assert anila_evs[0]["event"] == "anila.trace"
    assert anila_evs[0]["payload"] == payload


@pytest.mark.asyncio
@respx.mock
async def test_anila_meta_event_passes_through_unchanged() -> None:
    """Named ``event: anila.meta`` is the new template format. The previous
    parser dropped it because ``event:`` lines were ignored."""
    meta_payload = {
        "trace_id": "trace-abc",
        "trace": [{"label": "agent step"}],
        "follow_ups": ["a", "b"],
    }
    body = (
        'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        f"event: anila.meta\ndata: {json.dumps(meta_payload)}\n\n"
        "data: [DONE]\n\n"
    )
    respx.post(CSP_URL).mock(return_value=_sse_response(body))
    events = await _collect("a", "q")

    anila_evs = [e for e in events if e["type"] == "anila_event"]
    assert len(anila_evs) == 1
    assert anila_evs[0]["event"] == "anila.meta"
    assert anila_evs[0]["payload"] == meta_payload


@pytest.mark.asyncio
@respx.mock
async def test_anila_reasoning_event_passes_through() -> None:
    body = (
        'event: anila.reasoning\ndata: {"delta":"thinking..."}\n\n'
        "data: [DONE]\n\n"
    )
    respx.post(CSP_URL).mock(return_value=_sse_response(body))
    events = await _collect("a", "q")
    anila_evs = [e for e in events if e["type"] == "anila_event"]
    assert anila_evs == [
        {
            "type": "anila_event",
            "event": "anila.reasoning",
            "payload": {"delta": "thinking..."},
        }
    ]


# ---------------------------------------------------------------------------
# Sprint 9-12 typed events get renamed into the anila.* namespace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_interrupt_requested_renamed_to_anila_interrupt_requested() -> None:
    interrupt = {
        "interrupt_id": "int-1",
        "kind": "ask_user",
        "payload": {
            "question": "Pick one",
            "options": ["A", "B"],
            "multi_select": False,
            "allow_other": False,
        },
    }
    body = (
        'data: {"choices":[{"delta":{"content":"thinking"}}]}\n\n'
        f"event: interrupt_requested\ndata: {json.dumps(interrupt)}\n\n"
    )
    respx.post(CSP_URL).mock(return_value=_sse_response(body))
    events = await _collect("a", "q")

    interrupt_evs = [
        e for e in events
        if e["type"] == "anila_event"
        and e["event"] == "anila.interrupt_requested"
    ]
    assert len(interrupt_evs) == 1
    assert interrupt_evs[0]["payload"] == interrupt


@pytest.mark.asyncio
@respx.mock
async def test_todos_updated_renamed_and_payload_intact() -> None:
    todos = {
        "todos": [
            {"id": "t1", "content": "Read README", "status": "in_progress"},
            {"id": "t2", "content": "Write tests", "status": "pending"},
        ]
    }
    body = (
        f"event: todos_updated\ndata: {json.dumps(todos)}\n\n"
        "data: [DONE]\n\n"
    )
    respx.post(CSP_URL).mock(return_value=_sse_response(body))
    events = await _collect("a", "q")
    anila_evs = [e for e in events if e["type"] == "anila_event"]
    assert anila_evs == [
        {
            "type": "anila_event",
            "event": "anila.todos_updated",
            "payload": todos,
        }
    ]


@pytest.mark.asyncio
@respx.mock
async def test_follow_ups_event_renamed() -> None:
    payload = {"suggestions": ["next?", "deeper?", "summarise?"]}
    body = (
        f"event: follow_ups\ndata: {json.dumps(payload)}\n\n"
        "data: [DONE]\n\n"
    )
    respx.post(CSP_URL).mock(return_value=_sse_response(body))
    events = await _collect("a", "q")
    anila_evs = [e for e in events if e["type"] == "anila_event"]
    assert anila_evs == [
        {
            "type": "anila_event",
            "event": "anila.follow_ups",
            "payload": payload,
        }
    ]


@pytest.mark.asyncio
@respx.mock
async def test_tool_call_started_and_finished_pair() -> None:
    started = {"tool_call_id": "tc-1", "tool_name": "exec_python", "input": None}
    finished = {
        "tool_call_id": "tc-1",
        "tool_name": "exec_python",
        "is_error": False,
        "output_preview": "42",
    }
    body = (
        f"event: tool_call_started\ndata: {json.dumps(started)}\n\n"
        f"event: tool_call_finished\ndata: {json.dumps(finished)}\n\n"
        "data: [DONE]\n\n"
    )
    respx.post(CSP_URL).mock(return_value=_sse_response(body))
    events = await _collect("a", "q")
    anila_evs = [e for e in events if e["type"] == "anila_event"]
    names = [e["event"] for e in anila_evs]
    assert names == ["anila.tool_call_started", "anila.tool_call_finished"]
    assert anila_evs[0]["payload"] == started
    assert anila_evs[1]["payload"] == finished


@pytest.mark.asyncio
@respx.mock
async def test_resumed_event_renamed() -> None:
    body = (
        'event: resumed\ndata: {"interrupt_id":"int-9"}\n\n'
        'data: {"choices":[{"delta":{"content":"continuing"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    respx.post(CSP_URL).mock(return_value=_sse_response(body))
    events = await _collect("a", "q")
    assert events[0] == {
        "type": "anila_event",
        "event": "anila.resumed",
        "payload": {"interrupt_id": "int-9"},
    }
    assert events[1]["type"] == "content"


# ---------------------------------------------------------------------------
# SSE parser quirks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_unknown_named_event_is_ignored_not_misclassified() -> None:
    """An event name we don't recognise must not be mis-parsed as an OpenAI
    chunk. (The old parser would feed ``data:`` of an unknown event
    through the OpenAI path and silently fail to find ``choices``.)"""
    body = (
        'event: future_unknown\ndata: {"some":"payload"}\n\n'
        'data: {"choices":[{"delta":{"content":"text"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    respx.post(CSP_URL).mock(return_value=_sse_response(body))
    events = await _collect("a", "q")
    # Only the OpenAI delta + done. The unknown event is dropped because
    # it has neither an ``anila.`` prefix nor a recognised pass-through
    # name; it carries no OpenAI-shaped chunk so nothing surfaces.
    types = [e["type"] for e in events]
    assert types == ["content", "done"]


@pytest.mark.asyncio
@respx.mock
async def test_event_name_resets_between_messages() -> None:
    """After a dispatched SSE message, ``event:`` must reset to default so
    the next ``data:`` chunk parses as an OpenAI envelope, not as a
    follow-on of the previous named event."""
    body = (
        'event: anila.trace\ndata: {"label":"first"}\n\n'
        'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    respx.post(CSP_URL).mock(return_value=_sse_response(body))
    events = await _collect("a", "q")
    types = [e["type"] for e in events]
    assert types == ["anila_event", "content", "done"]


@pytest.mark.asyncio
@respx.mock
async def test_sse_comments_are_ignored() -> None:
    body = (
        ": heartbeat\n\n"
        ': keep-alive comment\n'
        'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    respx.post(CSP_URL).mock(return_value=_sse_response(body))
    events = await _collect("a", "q")
    assert [e["type"] for e in events] == ["content", "done"]


@pytest.mark.asyncio
@respx.mock
async def test_done_terminates_iteration() -> None:
    body = (
        "data: [DONE]\n\n"
        'data: {"choices":[{"delta":{"content":"after-done"}}]}\n\n'
    )
    respx.post(CSP_URL).mock(return_value=_sse_response(body))
    events = await _collect("a", "q")
    assert events == [{"type": "done"}]


@pytest.mark.asyncio
@respx.mock
async def test_http_error_surfaces_as_error_event() -> None:
    respx.post(CSP_URL).mock(
        return_value=httpx.Response(503, content=b"backend down")
    )
    events = await _collect("a", "q")
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert "503" in events[0]["error"]
    assert "backend down" in events[0]["detail"]


@pytest.mark.asyncio
@respx.mock
async def test_data_with_optional_space_after_colon() -> None:
    """SSE spec: ``data:hello`` and ``data: hello`` are both valid; the
    leading space after the colon is optional and stripped if present."""
    body = (
        'data:{"choices":[{"delta":{"content":"a"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"b"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    respx.post(CSP_URL).mock(return_value=_sse_response(body))
    events = await _collect("a", "q")
    deltas = [e["content"] for e in events if e["type"] == "content"]
    assert deltas == ["a", "b"]
