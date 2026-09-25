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

import asyncio
import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from anila_core.api import router_server
from anila_core.api.router_server import _stream_agent_sse
from anila_core.config import settings
from anila_core.memory import MemorySession
from anila_core.registry.remote_agent_manifest import (
    RemoteAgentManifest,
    RemoteAgentRegistry,
)


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
async def test_openai_message_stream_yields_content() -> None:
    """Some registered agents send complete assistant messages inside SSE."""
    body = (
        'data: {"choices":[{"message":{"role":"assistant","content":"hello from agent"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    respx.post(CSP_URL).mock(return_value=_sse_response(body))
    events = await _collect("a", "q")
    assert [e["type"] for e in events] == ["content", "done"]
    assert events[0]["content"] == "hello from agent"


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


@pytest.mark.asyncio
@respx.mock
async def test_stream_agent_sse_forwards_conversation_id() -> None:
    route = respx.post(CSP_URL).mock(return_value=_sse_response("data: [DONE]\n\n"))
    events: list[dict] = []
    async for ev in _stream_agent_sse(
        "a",
        "q",
        "k",
        forwarded_headers={"X-ANILA-Conversation-Id": "7"},
    ):
        events.append(ev)
    assert events == [{"type": "done"}]
    assert route.calls.last.request.headers.get("x-anila-conversation-id") == "7"
    assert route.calls.last.request.headers.get("authorization") == "Bearer k"


# ---------------------------------------------------------------------------
# Fail-closed: an upstream error frame is not a successful completion.
#
# CSP's chat proxy forwards agent SSE verbatim (it only rewrites
# ``anila.meta``). Two shapes therefore reach this parser unchanged:
#
#   * OpenAI's terminal error object on the default channel —
#     ``data: {"error": {"message": ..., "code": ...}}`` then ``[DONE]``.
#   * A named ``event: error`` frame (CSP's own resume passthrough uses
#     this name, and so do several agent runtimes).
#
# The Shell only treats ``event: anila.error`` with ``{"message": ...}``
# as a failed turn. Anything this parser drops is later closed by the
# dispatch loop as ``finish_reason=stop`` + ``[DONE]``, which the UI
# records as a successful answer. These tests pin the parser half of
# that contract: an error frame is a terminal ``type: error``, its
# private upstream text stays in ``detail`` (never the user-facing
# ``error`` string), and a stream that simply ends is not success.
# ---------------------------------------------------------------------------

_LEAK = "upstream secret sk-live-DO-NOT-LEAK at http://10.1.2.3/v1"


@pytest.mark.asyncio
@respx.mock
async def test_openai_error_object_after_delta_is_terminal_error() -> None:
    """Text delta, then a top-level ``{"error": ...}`` and ``[DONE]``.

    The error must surface as ``type: error`` *before* the done sentinel,
    and the upstream message must not become the user-facing string.
    """
    body = (
        'data: {"choices":[{"delta":{"content":"部分答案"}}]}\n\n'
        "data: "
        + json.dumps(
            {"error": {"message": _LEAK, "type": "server_error", "code": "internal"}}
        )
        + "\n\n"
        "data: [DONE]\n\n"
    )
    respx.post(CSP_URL).mock(return_value=_sse_response(body))
    events = await _collect("weather-agent", "q")

    assert [e["type"] for e in events] == ["content", "error"]
    assert events[0]["content"] == "部分答案"
    err = events[1]
    assert "weather-agent" in err["error"]
    # User-facing field is a fixed sentence; the raw upstream text is
    # operator-only and lives in ``detail``.
    assert _LEAK not in err["error"]
    assert "sk-live" not in err["error"]
    assert "10.1.2.3" not in err["error"]
    assert _LEAK in err["detail"]


@pytest.mark.asyncio
@respx.mock
async def test_named_event_error_frame_is_terminal_error() -> None:
    """``event: error`` is a real SSE name on this wire, not an OpenAI chunk."""
    body = (
        'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        "event: error\ndata: "
        + json.dumps({"status": 502, "detail": _LEAK})
        + "\n\n"
    )
    respx.post(CSP_URL).mock(return_value=_sse_response(body))
    events = await _collect("a", "q")

    assert [event["type"] for event in events] == ["content", "error"]
    assert events[0] == {"type": "content", "content": "hi"}
    assert _LEAK not in events[1]["error"]
    assert _LEAK in events[1]["detail"]


@pytest.mark.asyncio
@respx.mock
async def test_stream_eof_without_done_is_error_not_success() -> None:
    """The body ending after a delta (no ``[DONE]``, no error frame) is a
    truncated turn, not a clean completion."""
    body = 'data: {"choices":[{"delta":{"content":"半截"}}]}\n\n'
    respx.post(CSP_URL).mock(return_value=_sse_response(body))
    events = await _collect("a", "q")

    assert [e["type"] for e in events] == ["content", "error"]
    # Same fixed sentence as every other failure: the fact of the
    # truncation is recorded in ``detail``, not shown to the caller.
    assert "暫時無法使用" in events[1]["error"]
    assert "without data: [DONE]" in events[1]["detail"]


@pytest.mark.asyncio
@respx.mock
async def test_http_5xx_error_string_hides_response_body() -> None:
    """A 5xx body can carry stack traces and internal URLs. It stays in
    ``detail``; the user-facing ``error`` only names the status."""
    respx.post(CSP_URL).mock(
        return_value=httpx.Response(502, content=f"traceback: {_LEAK}".encode())
    )
    events = await _collect("a", "q")
    assert [e["type"] for e in events] == ["error"]
    assert "502" in events[0]["error"]
    assert _LEAK not in events[0]["error"]
    assert _LEAK in events[0]["detail"]


# ---------------------------------------------------------------------------
# Dispatch loop: a parsed agent failure must not be closed as success.
#
# ``_router_streaming`` ends its dispatch branch with ``anila.meta`` +
# ``finish_reason=stop`` + ``[DONE]`` unconditionally. The Shell reads
# ``stop`` as a completed answer and only ``event: anila.error`` (payload
# ``{"message"}``) as a failed turn, so an agent that streamed some text
# and then failed is recorded as a good answer. These tests drive the
# loop with the real parser output rather than a hand-built event.
# ---------------------------------------------------------------------------


class _OneAgent:
    def __init__(self, manifest: RemoteAgentManifest) -> None:
        self._manifest = manifest

    def get(self, _api_key: str, agent_id: str):
        return self._manifest if agent_id == self._manifest.agent_id else None


def _drive_dispatch(
    monkeypatch, agent_body: str, *, requires_encryption: bool = True
) -> str:
    """Run ``_router_streaming`` through one dispatch against a fake agent SSE."""

    async def fake_stream_llm(*_args, **_kwargs):
        yield {"type": "delta", "content": "DISPATCH:agent-a:查一下"}
        yield {"type": "done"}

    def fake_client():
        transport = httpx.MockTransport(
            lambda _request: _sse_response(agent_body)
        )
        return httpx.AsyncClient(transport=transport)

    monkeypatch.setattr(router_server, "_stream_llm_sse", fake_stream_llm)
    monkeypatch.setattr(router_server, "get_http_client", fake_client)

    manifest = RemoteAgentManifest(
        agent_id="agent-a",
        name="Agent A",
        description_for_router="a",
        endpoint_url="http://agent-a",
        requires_encryption=requires_encryption,
    )

    async def run() -> str:
        chunks: list[str] = []
        async for line in router_server._router_streaming(
            "sk",
            [{"role": "user", "content": "查一下"}],
            [{"role": "user", "content": "查一下"}],
            registry=_OneAgent(manifest),
            base_trace=[],
            started_at=0.0,
            route_signal=router_server._ROUTE_DIRECT,
        ):
            chunks.append(line)
        return "".join(chunks)

    return asyncio.run(run())


def _sse_events(body: str) -> list[tuple[str, str]]:
    """``(event name, data)`` per frame. Unnamed frames use ``""``."""
    out: list[tuple[str, str]] = []
    for block in body.split("\n\n"):
        name = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].lstrip()
        if data:
            out.append((name, data))
    return out


def test_dispatch_openai_error_after_delta_is_anila_error_not_stop(monkeypatch):
    body = (
        'data: {"choices":[{"delta":{"content":"部分答案"}}]}\n\n'
        "data: "
        + json.dumps({"error": {"message": _LEAK, "code": "internal"}})
        + "\n\n"
        "data: [DONE]\n\n"
    )
    rendered = _drive_dispatch(monkeypatch, body)
    frames = _sse_events(rendered)

    assert any("部分答案" in data for _name, data in frames)

    errors = [data for name, data in frames if name == "anila.error"]
    assert len(errors) == 1
    payload = json.loads(errors[0])
    assert set(payload) == {"message"}
    assert payload["message"]
    assert _LEAK not in payload["message"]
    assert "sk-live" not in rendered
    assert "10.1.2.3" not in rendered

    finishes = [
        json.loads(data)["choices"][0]["finish_reason"]
        for name, data in frames
        if name == "" and data != "[DONE]"
    ]
    assert "stop" not in finishes
    assert frames[-1] == ("anila.error", errors[0])


def test_dispatch_eof_is_anila_error_not_stop(monkeypatch):
    body = 'data: {"choices":[{"delta":{"content":"半截"}}]}\n\n'
    rendered = _drive_dispatch(monkeypatch, body)
    frames = _sse_events(rendered)

    assert any(name == "anila.error" for name, _data in frames)
    finishes = [
        json.loads(data)["choices"][0]["finish_reason"]
        for name, data in frames
        if name == "" and data != "[DONE]"
    ]
    assert "stop" not in finishes
    assert "[DONE]" not in rendered
    assert _LEAK not in rendered


@pytest.mark.parametrize(
    "agent_body",
    [
        (
            'data: {"choices":[{"delta":{"content":"部分答案"}}]}\n\n'
            'data: {"error":{"message":"' + _LEAK + '"}}\n\n'
            "data: [DONE]\n\n"
        ),
        'event: error\ndata: {"detail":"' + _LEAK + '"}\n\n',
        (
            'data: {"choices":[{"delta":{"content":"半截"}}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        ),
    ],
    ids=["in-band-error", "named-error", "stop-without-done"],
)
@respx.mock
def test_chat_completions_dispatch_error_is_terminal_and_redacted(
    monkeypatch, agent_body: str
) -> None:
    """The route's streaming dispatch branch has the same fail-closed contract."""
    async def _skip_registry_refresh(self, _api_key: str) -> None:
        return None

    async def _no_compaction(messages, **_kwargs):
        return messages, None, None

    monkeypatch.setattr(RemoteAgentRegistry, "ensure_fresh", _skip_registry_refresh)
    monkeypatch.setattr(router_server, "_auto_compact_routing_messages", _no_compaction)
    manifest = RemoteAgentManifest(
        agent_id="agent-a",
        name="Agent A",
        description_for_router="a",
        endpoint_url="http://agent-a",
        requires_encryption=True,
    )
    monkeypatch.setattr(RemoteAgentRegistry, "list_agents", lambda self, _api_key: [manifest])
    monkeypatch.setattr(
        RemoteAgentRegistry,
        "get",
        lambda self, _api_key, agent_id: manifest if agent_id == "agent-a" else None,
    )

    async def fake_stream_llm(*_args, **_kwargs):
        yield {"type": "delta", "content": "DISPATCH:agent-a:查一下\n"}
        yield {"type": "done"}

    monkeypatch.setattr(router_server, "_stream_llm_sse", fake_stream_llm)
    respx.post(CSP_URL).mock(return_value=_sse_response(agent_body))
    app = router_server.create_router_app(
        session_factory=lambda session_id: MemorySession(session_id)
    )
    response = TestClient(app).post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test"},
        json={
            "messages": [{"role": "user", "content": "查一下"}],
            "stream": True,
            "session_id": "s-sse-error",
        },
    )

    assert response.status_code == 200
    frames = _sse_events(response.text)
    assert any(name == "anila.error" for name, _data in frames)
    # Failure still tells the Shell this turn is classified. The meta is
    # only that flag (no usage on these bodies) and it precedes anila.error,
    # which the Shell treats as the last frame it will read.
    metas = [
        (i, json.loads(data))
        for i, (name, data) in enumerate(frames)
        if name == "anila.meta"
    ]
    assert len(metas) == 1
    meta_i, meta = metas[0]
    assert meta == {"classified": True}
    error_i = next(i for i, (name, _) in enumerate(frames) if name == "anila.error")
    assert meta_i < error_i
    assert frames[-1][0] == "anila.error"
    assert _LEAK not in response.text
    assert "sk-live" not in response.text
    assert "10.1.2.3" not in response.text
    assert not any(name == "" and data == "[DONE]" for name, data in frames)
    assert not any(
        name == "" and json.loads(data)["choices"][0]["finish_reason"] == "stop"
        for name, data in frames
    )
    traces = [json.loads(data) for name, data in frames if name == "anila.trace"]
    assert traces[-1]["detail"] == "agent「agent-a」暫時無法使用，請稍後再試。"
    assert _LEAK not in json.dumps(traces)


def test_upstream_anila_error_and_error_trace_do_not_reach_caller(monkeypatch):
    """An upstream ``anila.error`` and an error-status trace must not carry
    their text to the caller. The trace's user-visible fields are replaced;
    ``anila.error`` is the fixed outage sentence and ends the turn."""
    trace = {
        "kind": _LEAK,
        "label": _LEAK,
        "detail": _LEAK,
        "status": "error",
        "message": _LEAK,
        "latency_ms": 15,
    }
    body = (
        "event: anila.trace\ndata: "
        + json.dumps(trace, ensure_ascii=False)
        + "\n\n"
        + 'data: {"choices":[{"delta":{"content":"還在"}}]}\n\n'
        + "event: anila.error\ndata: "
        + json.dumps({"message": _LEAK, "trace": _LEAK}, ensure_ascii=False)
        + "\n\n"
        + "data: [DONE]\n\n"
    )
    rendered = _drive_dispatch(monkeypatch, body)
    assert _LEAK not in rendered
    assert "sk-live" not in rendered
    assert "10.1.2.3" not in rendered
    assert "還在" in rendered

    frames = _sse_events(rendered)
    traces = [json.loads(data) for name, data in frames if name == "anila.trace"]
    assert {
        "kind": "error",
        "label": "上游步驟失敗",
        "detail": "agent「agent-a」暫時無法使用，請稍後再試。",
        "status": "error",
        "latency_ms": 15,
    } in traces
    errors = [json.loads(data) for name, data in frames if name == "anila.error"]
    assert errors == [{"message": "agent「agent-a」暫時無法使用，請稍後再試。"}]
    assert frames[-1][0] == "anila.error"
    assert not any(name == "" and data == "[DONE]" for name, data in frames)
    assert not any(
        name == ""
        and data != "[DONE]"
        and json.loads(data)["choices"][0]["finish_reason"] == "stop"
        for name, data in frames
    )


def test_dispatch_failure_meta_carries_classified_and_usage_before_error(monkeypatch):
    """A failed dispatch still tells the Shell ``classified`` and any
    trustworthy usage, and that meta is not a success frame. ``anila.error``
    stays last because the Shell stops reading there."""
    usage = {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9}
    upstream_meta = {
        "usage": {**usage, "note": _LEAK},
        "citations": [_LEAK],
        "handoff_chain": [{"agent_id": "x", "output_summary": _LEAK}],
        "route": {"decision": "dispatch"},
        "answering_agent_id": "agent-a",
        "confidence": 0.9,
    }
    body = (
        "event: anila.meta\ndata: "
        + json.dumps(upstream_meta, ensure_ascii=False)
        + "\n\n"
        + 'data: {"choices":[{"delta":{"content":"部分"}}]}\n\n'
        + "data: "
        + json.dumps({"error": {"message": _LEAK, "code": "internal"}})
        + "\n\n"
        + "data: [DONE]\n\n"
    )
    rendered = _drive_dispatch(monkeypatch, body)
    assert _LEAK not in rendered
    assert "部分" in rendered

    frames = _sse_events(rendered)
    metas = [
        (i, json.loads(data))
        for i, (name, data) in enumerate(frames)
        if name == "anila.meta"
    ]
    assert len(metas) == 1
    meta_i, meta = metas[0]
    assert meta == {"classified": True, "usage": usage}
    error_i = next(i for i, (name, _) in enumerate(frames) if name == "anila.error")
    assert meta_i < error_i
    assert frames[-1][0] == "anila.error"
    assert not any(name == "" and data == "[DONE]" for name, data in frames)


def test_upstream_non_error_trace_and_spans_hide_diagnostic_secrets(monkeypatch):
    """A trace whose status is not ``error``, and an upstream span, can
    still carry an exception or address. Those strings must not be forwarded."""
    trace = {
        "kind": "tool",
        "label": _LEAK,
        "detail": f"finished {_LEAK}",
        "status": "ok",
    }
    spans = {
        "spans": [
            {
                "span_id": "span-public",
                "span_type": "agent.tool_call.finished",
                "name": _LEAK,
                "status": "ok",
                "attributes": {"target": "agent-a", "error": _LEAK},
            }
        ]
    }
    body = (
        "event: anila.trace\ndata: "
        + json.dumps(trace, ensure_ascii=False)
        + "\n\n"
        + "event: anila.spans\ndata: "
        + json.dumps(spans, ensure_ascii=False)
        + "\n\n"
        + 'data: {"choices":[{"delta":{"content":"答案"}}]}\n\n'
        + "data: [DONE]\n\n"
    )
    rendered = _drive_dispatch(monkeypatch, body)
    assert _LEAK not in rendered
    assert "sk-live" not in rendered
    assert "10.1.2.3" not in rendered
    assert "答案" in rendered
    assert "data: [DONE]" in rendered
    assert "event: anila.error" not in rendered

    frames = _sse_events(rendered)
    traces = [json.loads(data) for name, data in frames if name == "anila.trace"]
    assert {
        "kind": "tool",
        "label": "已省略上游診斷內容",
        "detail": "已省略上游診斷內容",
        "status": "ok",
    } in traces
    span_frames = [json.loads(data) for name, data in frames if name == "anila.spans"]
    assert span_frames
    span = span_frames[0]["spans"][0]
    assert span["span_id"] == "span-public"
    assert span["attributes"]["target"] == "agent-a"
    assert span["attributes"]["error"] == "已省略上游診斷內容"
    assert span["name"] == "已省略上游診斷內容"


def test_failure_meta_keeps_classified_latched_by_agent_meta(monkeypatch):
    """Manifest unclassified, but the agent already sent ``classified: true``."""
    body = (
        "event: anila.meta\ndata: "
        + json.dumps(
            {"classified": True, "citations": [_LEAK]}, ensure_ascii=False
        )
        + "\n\n"
        + 'data: {"choices":[{"delta":{"content":"部分"}}]}\n\n'
        + "data: "
        + json.dumps({"error": {"message": _LEAK}})
        + "\n\n"
    )
    rendered = _drive_dispatch(monkeypatch, body, requires_encryption=False)
    assert _LEAK not in rendered
    frames = _sse_events(rendered)
    metas = [json.loads(data) for name, data in frames if name == "anila.meta"]
    assert metas == [{"classified": True}]
    assert frames[-1][0] == "anila.error"
    meta_i = next(i for i, (name, _) in enumerate(frames) if name == "anila.meta")
    error_i = next(i for i, (name, _) in enumerate(frames) if name == "anila.error")
    assert meta_i < error_i


def test_non_string_reasoning_tokens_source_does_not_abort_the_answer(monkeypatch):
    """``reasoning_tokens_source: []`` must not raise and end a good turn."""
    usage = {
        "prompt_tokens": 3,
        "completion_tokens": 4,
        "total_tokens": 7,
        "reasoning_tokens_source": [_LEAK],
    }
    body = (
        "data: "
        + json.dumps(
            {
                "choices": [{"delta": {"content": "答案"}, "finish_reason": None}],
                "usage": usage,
            },
            ensure_ascii=False,
        )
        + "\n\n"
        + "data: [DONE]\n\n"
    )
    rendered = _drive_dispatch(monkeypatch, body)
    assert _LEAK not in rendered
    assert "答案" in rendered
    assert "data: [DONE]" in rendered
    assert "event: anila.error" not in rendered
    assert "TypeError" not in rendered
    assert "unhashable" not in rendered


def test_successful_trace_redacts_only_the_url(monkeypatch):
    """A normal trace that mentions a URL keeps the surrounding words."""
    detail = "參考 https://docs.example.org/help"
    body = (
        "event: anila.trace\ndata: "
        + json.dumps(
            {"kind": "tool", "label": "查詢", "detail": detail, "status": "ok"},
            ensure_ascii=False,
        )
        + "\n\n"
        + 'data: {"choices":[{"delta":{"content":"好"}}]}\n\n'
        + "data: [DONE]\n\n"
    )
    rendered = _drive_dispatch(monkeypatch, body)
    assert "https://docs.example.org" not in rendered
    assert "docs.example.org" not in rendered
    assert "參考" in rendered
    frames = _sse_events(rendered)
    traces = [json.loads(data) for name, data in frames if name == "anila.trace"]
    assert any(
        step.get("label") == "查詢" and str(step.get("detail", "")).startswith("參考")
        for step in traces
    )
    assert "data: [DONE]" in rendered
