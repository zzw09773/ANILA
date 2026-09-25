"""Router Full-Trace production tests (Slice 4b).

Verifies the router creates dispatch spans and mirrors them into the
``anila.spans`` SSE event WHEN tracing is configured, and does nothing
(no spans, no event) when it is not — i.e. zero behaviour change without
``ANILA_TRACE_ENDPOINT``.

Networkless: the CSP-facing helpers (``_stream_llm_sse`` /
``_stream_agent_sse`` / ``_call_llm_non_stream`` / ``dispatch_to_agent_response``
/ ``_recompose_reply``) and the registry are monkeypatched, and the
exporter is a pure in-memory fake. No httpx, no threads.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest_asyncio
from fastapi.testclient import TestClient

from anila_core.api import router_server
from anila_core.memory import close_all_connections
from anila_core.registry.remote_agent_manifest import (
    RemoteAgentManifest,
    RemoteAgentRegistry,
)
from anila_core.tracing.sdk import TraceSession


@pytest_asyncio.fixture
async def db_path(tmp_path: Path):
    db = tmp_path / "router-trace.db"
    yield db
    await close_all_connections()


class FakeExporter:
    """Records enqueued spans; satisfies the TraceExporter.enqueue contract."""

    def __init__(self) -> None:
        self.spans: list[tuple[str, dict]] = []

    def enqueue(self, trace_id: str, span: dict) -> None:
        self.spans.append((trace_id, span))


AGENT = RemoteAgentManifest(
    agent_id="agent-a",
    name="Agent A",
    description_for_router="Specialist A",
    endpoint_url="http://agent-a",
    requires_encryption=False,
)


def _patch_registry(monkeypatch) -> None:
    async def fake_ensure_fresh(self, api_key: str) -> None:
        return None

    monkeypatch.setattr(RemoteAgentRegistry, "ensure_fresh", fake_ensure_fresh)
    monkeypatch.setattr(RemoteAgentRegistry, "list_agents", lambda self, k: [AGENT])
    monkeypatch.setattr(RemoteAgentRegistry, "get", lambda self, k, aid: AGENT)


def _install_fake_session(monkeypatch) -> FakeExporter:
    exporter = FakeExporter()

    def fake_make(trace_id):
        if not trace_id:
            return None
        return TraceSession(exporter, trace_id, producer="anila-router")

    monkeypatch.setattr(router_server, "_make_trace_session", fake_make)
    return exporter


def _parse_sse(body: str) -> list[dict]:
    out: list[dict] = []
    for block in body.strip().split("\n\n"):
        name = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if not data or data == "[DONE]":
            continue
        try:
            out.append({"event": name or "chunk", "data": json.loads(data)})
        except json.JSONDecodeError:
            pass
    return out


# ---------------------------------------------------------------------------
# Streaming dispatch → anila.spans SSE event
# ---------------------------------------------------------------------------


def test_streaming_dispatch_emits_anila_spans_when_configured(
    monkeypatch, db_path: Path
) -> None:
    _patch_registry(monkeypatch)
    exporter = _install_fake_session(monkeypatch)

    async def fake_stream_llm(api_key, messages, *, forwarded_headers=None, **_kwargs):
        yield {"type": "delta", "content": "DISPATCH:agent-a:hello"}
        yield {"type": "done"}

    async def fake_stream_agent(agent_id, query, api_key, *, session_id=None, forwarded_headers=None):
        yield {"type": "content", "content": "hi from agent"}
        yield {"type": "done"}

    async def fake_recompose(content, api_key, *, forwarded_headers=None):
        return content, "skipped"

    monkeypatch.setattr(router_server, "_stream_llm_sse", fake_stream_llm)
    monkeypatch.setattr(router_server, "_stream_agent_sse", fake_stream_agent)
    monkeypatch.setattr(router_server, "_recompose_reply", fake_recompose)

    app = router_server.create_router_app(session_db_path=str(db_path))
    client = TestClient(app)
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-x", "X-ANILA-Trace-Id": "trace-123"},
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)

    spans_events = [e for e in events if e["event"] == "anila.spans"]
    assert len(spans_events) == 1
    spans = spans_events[0]["data"]["spans"]
    assert [s["span_type"] for s in spans] == [
        "agent.run.finished",
        "agent.model_call.finished",
    ]
    decision, downstream = spans
    # Shape (FROZEN wire keys) + timing + parentage.
    for s in spans:
        assert s["span_id"] and s["name"] and s["started_at"] and s["ended_at"]
        assert s["status"] == "ok"
    assert "parent_span_id" not in decision
    assert downstream["parent_span_id"] == decision["span_id"]
    assert decision["attributes"]["chosen_agent"] == "agent-a"
    assert downstream["attributes"]["target"] == "agent-a"

    # Same span dicts were also shipped to the exporter (callback path).
    assert len(exporter.spans) == 2
    assert {t for t, _ in exporter.spans} == {"trace-123"}
    assert [sp["span_type"] for _, sp in exporter.spans] == [
        "agent.model_call.finished",  # child closes first
        "agent.run.finished",
    ]


def test_streaming_dispatch_failure_closes_spans_before_anila_error(
    monkeypatch, db_path: Path
) -> None:
    """A mid-stream agent failure closes both open spans with the safe
    sentence, emits ``anila.spans``, and only then the terminal
    ``anila.error``. The Shell stops at that frame, so a later spans
    event would never be applied."""
    secret = "sk-live-SPAN-DO-NOT-LEAK"
    manifest = RemoteAgentManifest(
        agent_id="agent-a",
        name="Agent A",
        description_for_router="Specialist A",
        endpoint_url="http://agent-a",
        requires_encryption=True,
    )

    async def fake_ensure_fresh(self, api_key: str) -> None:
        return None

    monkeypatch.setattr(RemoteAgentRegistry, "ensure_fresh", fake_ensure_fresh)
    monkeypatch.setattr(RemoteAgentRegistry, "list_agents", lambda self, k: [manifest])
    monkeypatch.setattr(
        RemoteAgentRegistry,
        "get",
        lambda self, k, aid: manifest if aid == "agent-a" else None,
    )
    exporter = _install_fake_session(monkeypatch)

    async def fake_stream_llm(api_key, messages, *, forwarded_headers=None, **_kwargs):
        yield {"type": "delta", "content": "DISPATCH:agent-a:hello"}
        yield {"type": "done"}

    async def fake_stream_agent(agent_id, query, api_key, *, session_id=None, forwarded_headers=None):
        yield {"type": "content", "content": "partial answer"}
        yield {
            "type": "anila_event",
            "event": "anila.trace",
            "payload": {
                "kind": "tool",
                "label": secret,
                "detail": secret,
                "status": "error",
                "latency_ms": 8,
            },
        }
        yield {
            "type": "error",
            "error": "agent「agent-a」暫時無法使用，請稍後再試。",
            "detail": secret,
        }

    monkeypatch.setattr(router_server, "_stream_llm_sse", fake_stream_llm)
    monkeypatch.setattr(router_server, "_stream_agent_sse", fake_stream_agent)

    app = router_server.create_router_app(session_db_path=str(db_path))
    client = TestClient(app)
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-x", "X-ANILA-Trace-Id": "trace-fail"},
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert resp.status_code == 200
    assert secret not in resp.text
    assert "partial answer" in resp.text
    assert "data: [DONE]" not in resp.text
    assert '"finish_reason": "stop"' not in resp.text

    events = _parse_sse(resp.text)
    names = [e["event"] for e in events]
    span_at = names.index("anila.spans")
    err_at = names.index("anila.error")
    assert span_at < err_at
    assert names[-1] == "anila.error"

    spans = next(e for e in events if e["event"] == "anila.spans")["data"]["spans"]
    assert [s["span_type"] for s in spans] == [
        "agent.run.finished",
        "agent.model_call.finished",
    ]
    safe = "agent「agent-a」暫時無法使用，請稍後再試。"
    decision, downstream = spans
    for span in spans:
        assert span["status"] == "error"
        assert span["ended_at"]
        assert span["attributes"]["error"] == safe
        assert secret not in json.dumps(span)
    assert downstream["parent_span_id"] == decision["span_id"]

    traces = [e["data"] for e in events if e["event"] == "anila.trace"]
    assert {
        "kind": "tool",
        "label": "上游步驟失敗",
        "detail": safe,
        "status": "error",
        "latency_ms": 8,
    } in traces

    assert len(exporter.spans) == 2
    assert {t for t, _ in exporter.spans} == {"trace-fail"}
    assert [sp["span_type"] for _, sp in exporter.spans] == [
        "agent.model_call.finished",
        "agent.run.finished",
    ]
    assert [sp["status"] for _, sp in exporter.spans] == ["error", "error"]


def test_streaming_dispatch_emits_no_spans_when_unconfigured(
    monkeypatch, db_path: Path
) -> None:
    _patch_registry(monkeypatch)
    # No exporter configured → ensure the real gate returns None.
    monkeypatch.delenv("ANILA_TRACE_ENDPOINT", raising=False)

    async def fake_stream_llm(api_key, messages, *, forwarded_headers=None, **_kwargs):
        yield {"type": "delta", "content": "DISPATCH:agent-a:hello"}
        yield {"type": "done"}

    async def fake_stream_agent(agent_id, query, api_key, *, session_id=None, forwarded_headers=None):
        yield {"type": "content", "content": "hi"}
        yield {"type": "done"}

    async def fake_recompose(content, api_key, *, forwarded_headers=None):
        return content, "skipped"

    monkeypatch.setattr(router_server, "_stream_llm_sse", fake_stream_llm)
    monkeypatch.setattr(router_server, "_stream_agent_sse", fake_stream_agent)
    monkeypatch.setattr(router_server, "_recompose_reply", fake_recompose)

    app = router_server.create_router_app(session_db_path=str(db_path))
    client = TestClient(app)
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-x", "X-ANILA-Trace-Id": "trace-xyz"},
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert resp.status_code == 200
    assert "event: anila.spans" not in resp.text
    # Existing contract preserved: still get trace + meta + DONE.
    assert "event: anila.meta" in resp.text
    assert "data: [DONE]" in resp.text


# ---------------------------------------------------------------------------
# Non-streaming dispatch → exporter callback spans
# ---------------------------------------------------------------------------


def test_non_streaming_dispatch_records_spans_when_configured(
    monkeypatch, db_path: Path
) -> None:
    _patch_registry(monkeypatch)
    exporter = _install_fake_session(monkeypatch)

    async def fake_call_llm(api_key, messages, *, forwarded_headers=None, **_kwargs):
        return {"content": "DISPATCH:agent-a:hello", "reasoning": None,
                "anila_meta": None, "raw": None, "error": None}

    async def fake_dispatch(**kwargs):
        return {"content": "answer", "anila_meta": None, "raw": None}

    async def fake_recompose(content, api_key, *, forwarded_headers=None):
        return content, "skipped"

    monkeypatch.setattr(router_server, "_call_llm_non_stream", fake_call_llm)
    monkeypatch.setattr(router_server, "dispatch_to_agent_response", fake_dispatch)
    monkeypatch.setattr(router_server, "_recompose_reply", fake_recompose)

    app = router_server.create_router_app(session_db_path=str(db_path))
    client = TestClient(app)
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-x", "X-ANILA-Trace-Id": "trace-ns"},
        json={"messages": [{"role": "user", "content": "hi"}], "stream": False},
    )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "answer"

    types = [sp["span_type"] for _, sp in exporter.spans]
    assert types == ["agent.model_call.finished", "agent.run.finished"]
    trace_ids = {t for t, _ in exporter.spans}
    assert trace_ids == {"trace-ns"}
    # downstream child references the decision (run) span as parent.
    by_type = {sp["span_type"]: sp for _, sp in exporter.spans}
    assert (
        by_type["agent.model_call.finished"]["parent_span_id"]
        == by_type["agent.run.finished"]["span_id"]
    )


def test_non_streaming_dispatch_no_spans_when_unconfigured(
    monkeypatch, db_path: Path
) -> None:
    _patch_registry(monkeypatch)
    monkeypatch.delenv("ANILA_TRACE_ENDPOINT", raising=False)

    async def fake_call_llm(api_key, messages, *, forwarded_headers=None, **_kwargs):
        return {"content": "DISPATCH:agent-a:hello", "reasoning": None,
                "anila_meta": None, "raw": None, "error": None}

    async def fake_dispatch(**kwargs):
        return {"content": "answer", "anila_meta": None, "raw": None}

    async def fake_recompose(content, api_key, *, forwarded_headers=None):
        return content, "skipped"

    monkeypatch.setattr(router_server, "_call_llm_non_stream", fake_call_llm)
    monkeypatch.setattr(router_server, "dispatch_to_agent_response", fake_dispatch)
    monkeypatch.setattr(router_server, "_recompose_reply", fake_recompose)

    app = router_server.create_router_app(session_db_path=str(db_path))
    client = TestClient(app)
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-x", "X-ANILA-Trace-Id": "trace-ns2"},
        json={"messages": [{"role": "user", "content": "hi"}], "stream": False},
    )
    # No exception, normal answer, and nothing traced.
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "answer"


# ---------------------------------------------------------------------------
# Config gate — _trace_endpoint_base / _make_trace_session
# ---------------------------------------------------------------------------


def test_trace_endpoint_base_flag_uses_csp_base(monkeypatch) -> None:
    from anila_core.config import settings

    monkeypatch.setenv("ANILA_TRACE_ENDPOINT", "1")
    assert router_server._trace_endpoint_base() == settings.csp_base_url
    monkeypatch.setenv("ANILA_TRACE_ENDPOINT", "https://trace.example")
    assert router_server._trace_endpoint_base() == "https://trace.example"
    monkeypatch.delenv("ANILA_TRACE_ENDPOINT", raising=False)
    assert router_server._trace_endpoint_base() is None


def test_make_trace_session_disabled_returns_none(monkeypatch) -> None:
    monkeypatch.delenv("ANILA_TRACE_ENDPOINT", raising=False)
    assert router_server._make_trace_session("t") is None
    # Configured but no trace_id → still None (nothing to correlate).
    monkeypatch.setattr(router_server, "_get_trace_exporter", lambda: FakeExporter())
    assert router_server._make_trace_session(None) is None
    assert router_server._make_trace_session("t") is not None
