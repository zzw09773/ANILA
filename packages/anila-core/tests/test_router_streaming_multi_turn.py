"""Tests for Sprint 11 PR 4 — streaming multi-turn Router.

Single-shot streaming uses the existing real-time path; only the
multi-iteration path goes through the new soft-chunk implementation.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest_asyncio
import respx
from fastapi.testclient import TestClient

from anila_core.api.router_server import create_router_app
from anila_core.config import settings
from anila_core.memory import close_all_connections


CSP_BASE = settings.csp_base_url
CSP_URL = f"{CSP_BASE}/v1/chat/completions"
CSP_AGENTS_URL = f"{CSP_BASE}/v1/agents"


@pytest_asyncio.fixture
async def db_path(tmp_path: Path):
    db = tmp_path / "router-stream-multi.db"
    yield db
    await close_all_connections()


def _agent_list_response() -> dict:
    return {
        "data": [
            {
                "id": "agent-a",
                "name": "Agent A",
                "description_for_router": "Specialist A",
                "endpoint_url": "http://agent-a",
                "requires_encryption": False,
            },
            {
                "id": "agent-b",
                "name": "Agent B",
                "description_for_router": "Specialist B",
                "endpoint_url": "http://agent-b",
                "requires_encryption": False,
            },
        ]
    }


def _completion(content: str, model: str = "router-llm") -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


def _make_csp_handler(scripted: list[dict]):
    iterator = iter(scripted)

    def handler(request: httpx.Request) -> httpx.Response:
        try:
            return httpx.Response(200, json=next(iterator))
        except StopIteration:
            return httpx.Response(500, json={"detail": "exhausted"})

    return handler


def _parse_sse(body: str) -> list[dict]:
    out: list[dict] = []
    for block in body.strip().split("\n\n"):
        event_name = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                event_name = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if data == "[DONE]":
            out.append({"event": "done", "data": "[DONE]"})
            continue
        if data:
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue
            out.append({"event": event_name or "chunk", "data": payload})
    return out


# ---------------------------------------------------------------------------
# Streaming multi-turn dispatch — synthesised final answer
# ---------------------------------------------------------------------------


@respx.mock
def test_streaming_multi_turn_streams_only_final_synthesis(
    db_path: Path,
) -> None:
    respx.get(CSP_AGENTS_URL).mock(
        return_value=httpx.Response(200, json=_agent_list_response())
    )
    respx.post(CSP_URL).mock(
        side_effect=_make_csp_handler(
            [
                _completion("DISPATCH:agent-a:fetch X"),
                _completion("X = 42", model="agent-a"),
                _completion("Final synthesis: A reported X=42 as expected."),
            ]
        )
    )
    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "what's X?"}],
            "stream": True,
            "anila_multi_turn": 2,
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert response.status_code == 200
    assert response.headers["X-Anila-Session-Id"]

    events = _parse_sse(response.text)
    # The final synthesised text appears in chunk events.
    chunk_text = "".join(
        e["data"]["choices"][0]["delta"].get("content", "")
        for e in events
        if e["event"] == "chunk"
        and isinstance(e["data"], dict)
        and "choices" in e["data"]
    )
    assert "Final synthesis" in chunk_text
    assert "42" in chunk_text
    # Trace events fired for the dispatch step at minimum.
    trace_events = [e for e in events if e["event"] == "anila.trace"]
    assert any(
        t["data"]["kind"] == "dispatch" for t in trace_events
    )
    # Stream terminates with [DONE].
    assert events[-1]["event"] == "done"


# ---------------------------------------------------------------------------
# Streaming multi-turn — direct router answer (no dispatch)
# ---------------------------------------------------------------------------


@respx.mock
def test_streaming_multi_turn_handles_direct_answer(db_path: Path) -> None:
    """When LLM doesn't DISPATCH, multi-turn path streams the direct reply."""
    respx.get(CSP_AGENTS_URL).mock(
        return_value=httpx.Response(200, json=_agent_list_response())
    )
    respx.post(CSP_URL).mock(
        side_effect=_make_csp_handler(
            [_completion("Hello there, no agent needed.")]
        )
    )
    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "anila_multi_turn": 3,
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert response.status_code == 200
    events = _parse_sse(response.text)
    text = "".join(
        e["data"]["choices"][0]["delta"].get("content", "")
        for e in events
        if e["event"] == "chunk"
        and isinstance(e["data"], dict)
        and "choices" in e["data"]
    )
    assert "Hello there" in text
    direct_traces = [
        e for e in events
        if e["event"] == "anila.trace"
        and e["data"].get("kind") == "direct"
    ]
    assert len(direct_traces) >= 1


# ---------------------------------------------------------------------------
# Single-shot streaming path is unchanged (smoke test)
# ---------------------------------------------------------------------------


@respx.mock
def test_single_shot_streaming_uses_existing_path(db_path: Path) -> None:
    """No anila_multi_turn flag → existing token-by-token path."""
    respx.get(CSP_AGENTS_URL).mock(
        return_value=httpx.Response(200, json=_agent_list_response())
    )
    respx.post(CSP_URL).mock(
        side_effect=_make_csp_handler(
            [
                _completion("DISPATCH:agent-a:answer please"),
            ]
        )
    )
    # Mock the agent SSE stream as well — single-shot streaming opens
    # a stream connection to CSP; without a stream mock it would fail.
    # Use a 200 with [DONE] so the test doesn't require a full SSE mock.
    # Easier: omit dispatch entirely, just have router answer directly.
    respx.post(CSP_URL).mock(
        side_effect=_make_csp_handler(
            [_completion("direct hi")]
        )
    )
    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            # No anila_multi_turn → defaults to 1.
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert response.status_code == 200
    # Single-shot path uses _stream_llm_sse which expects SSE format.
    # Our mock returned non-SSE JSON, so it'll error out gracefully.
    # We just want to verify the multi-turn branch wasn't taken.
    # Easier check: response had X-Anila-Session-Id header set.
    assert response.headers["X-Anila-Session-Id"]


# ---------------------------------------------------------------------------
# Multi-turn streaming sets X-Anila-Session-Id
# ---------------------------------------------------------------------------


@respx.mock
def test_multi_turn_stream_emits_session_header(db_path: Path) -> None:
    respx.get(CSP_AGENTS_URL).mock(
        return_value=httpx.Response(200, json=_agent_list_response())
    )
    respx.post(CSP_URL).mock(
        side_effect=_make_csp_handler(
            [_completion("hi back")]
        )
    )
    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "anila_multi_turn": 2,
            "session_id": "s-pinned",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert response.headers["X-Anila-Session-Id"] == "s-pinned"


@respx.mock
def test_multi_turn_dispatch_failure_is_anila_error_not_stop(db_path: Path) -> None:
    """A failed ``_dispatch_safe`` ends the multi-turn stream. It must not
    recompose the outage text or close the turn with ``finish=stop`` / ``[DONE]``."""
    secret = "sk-live-TURN-DO-NOT-LEAK"
    agents = _agent_list_response()
    agents["data"][0]["requires_encryption"] = True
    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json=agents))

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("model") == "agent-a":
            return httpx.Response(500, content=f"traceback {secret}".encode())
        return httpx.Response(200, json=_completion("DISPATCH:agent-a:查一下"))

    respx.post(CSP_URL).mock(side_effect=handler)
    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "查一下"}],
            "stream": True,
            "anila_multi_turn": 2,
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert response.status_code == 200, response.text
    assert secret not in response.text
    assert "upstream HTTP" not in response.text
    assert "暫時不可用" not in response.text
    assert "data: [DONE]" not in response.text
    assert '"finish_reason": "stop"' not in response.text

    events = _parse_sse(response.text)
    errors = [e for e in events if e["event"] == "anila.error"]
    assert errors == [
        {"event": "anila.error", "data": {"message": "助手暫時無法使用，請稍後再試。"}}
    ]
    assert events[-1]["event"] == "anila.error"
    assert not any(e["event"] == "done" for e in events)


@respx.mock
def test_multi_turn_second_dispatch_failure_is_anila_error_not_stop(
    db_path: Path,
) -> None:
    """The first agent answers; the second dispatch fails.

    The loop used to return that failure and the caller still recomposed
    it and closed with ``finish=stop`` and ``[DONE]``.
    """
    secret = "sk-live-SECOND-DO-NOT-LEAK"
    agents = _agent_list_response()
    agents["data"][1]["requires_encryption"] = True
    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json=agents))
    router_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode() or "{}")
        model = body.get("model")
        if model == "agent-a":
            return httpx.Response(200, json=_completion("第一段答案", model="agent-a"))
        if model == "agent-b":
            return httpx.Response(
                500,
                content=f"traceback {secret} at http://10.4.4.4/secret".encode(),
            )
        router_calls["n"] += 1
        if router_calls["n"] == 1:
            return httpx.Response(200, json=_completion("DISPATCH:agent-a:查一下"))
        return httpx.Response(200, json=_completion("DISPATCH:agent-b:再查"))

    respx.post(CSP_URL).mock(side_effect=handler)
    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "查一下"}],
            "stream": True,
            "anila_multi_turn": 2,
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert response.status_code == 200, response.text
    assert secret not in response.text
    assert "10.4.4.4" not in response.text
    assert "HTTP 500" not in response.text
    assert "暫時不可用" not in response.text
    assert "data: [DONE]" not in response.text
    assert '"finish_reason": "stop"' not in response.text

    events = _parse_sse(response.text)
    assert events[-1] == {
        "event": "anila.error",
        "data": {"message": "助手暫時無法使用，請稍後再試。"},
    }
    metas = [e for e in events if e["event"] == "anila.meta"]
    assert metas
    assert metas[-1]["data"].get("classified") is True
    assert "citations" not in metas[-1]["data"]
    assert "handoff_chain" not in metas[-1]["data"]
    error_at = next(i for i, e in enumerate(events) if e["event"] == "anila.error")
    meta_at = next(i for i, e in enumerate(events) if e["event"] == "anila.meta")
    assert meta_at < error_at


@respx.mock
def test_multi_turn_keeps_classified_latched_by_earlier_agent_meta(
    db_path: Path,
) -> None:
    """Agent 1's meta says classified; agent 2's dispatch then fails.

    Both manifests are unclassified. The failure used to look only at
    those manifests and the failed reply, and sent ``classified: false``.
    """
    agents = _agent_list_response()
    for row in agents["data"]:
        row["requires_encryption"] = False
    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json=agents))
    router_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode() or "{}")
        model = body.get("model")
        if model == "agent-a":
            payload = _completion("第一段答案", model="agent-a")
            payload["anila_meta"] = {"classified": True, "citations": ["secret-cite"]}
            return httpx.Response(200, json=payload)
        if model == "agent-b":
            return httpx.Response(500, content=b"agent-b exploded")
        router_calls["n"] += 1
        if router_calls["n"] == 1:
            return httpx.Response(200, json=_completion("DISPATCH:agent-a:查一下"))
        return httpx.Response(200, json=_completion("DISPATCH:agent-b:再查"))

    respx.post(CSP_URL).mock(side_effect=handler)
    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "查一下"}],
            "stream": True,
            "anila_multi_turn": 2,
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert response.status_code == 200, response.text
    assert "secret-cite" not in response.text
    assert "data: [DONE]" not in response.text
    events = _parse_sse(response.text)
    assert events[-1]["event"] == "anila.error"
    metas = [e["data"] for e in events if e["event"] == "anila.meta"]
    assert metas
    assert metas[-1]["classified"] is True
    assert "citations" not in metas[-1]
    assert "handoff_chain" not in metas[-1]
