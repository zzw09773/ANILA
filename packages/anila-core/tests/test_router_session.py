"""Tests for Sprint 10 PR 3 — Session-aware Router.

Verifies:

- ``X-Anila-Session-Id`` header surfaces on every response (auto-generated
  when caller doesn't pass one, echoed when they do)
- Caller-supplied ``session_id`` / ``anila_session_id`` is honored
- Dispatch CSP requests carry the same ``anila_session_id`` extension
- ``GET /v1/sessions/{id}/state`` returns history + pending interrupts
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
from anila_core.memory import MemorySession, close_all_connections


CSP_BASE = settings.csp_base_url
CSP_URL = f"{CSP_BASE}/v1/chat/completions"
CSP_AGENTS_URL = f"{CSP_BASE}/v1/agents"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_path(tmp_path: Path):
    db = tmp_path / "router-sessions.db"
    yield db
    await close_all_connections()


def _llm_router_response(content: str) -> dict:
    """A non-streaming OpenAI-compatible reply we make CSP return when the
    Router calls the primary LLM for routing decision."""
    return {
        "id": "chatcmpl-r",
        "object": "chat.completion",
        "created": 0,
        "model": "router-llm",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


# ---------------------------------------------------------------------------
# X-Anila-Session-Id header
# ---------------------------------------------------------------------------


@respx.mock
def test_router_generates_session_id_when_caller_omits(
    db_path: Path,
) -> None:
    respx.post(CSP_URL).mock(
        return_value=httpx.Response(
            200, json=_llm_router_response("Hi back!")
        )
    )
    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "say hi"}],
            "stream": False,
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert response.status_code == 200
    sid = response.headers.get("X-Anila-Session-Id")
    assert sid and len(sid) >= 8


@respx.mock
def test_router_echoes_caller_supplied_session_id(db_path: Path) -> None:
    respx.post(CSP_URL).mock(
        return_value=httpx.Response(
            200, json=_llm_router_response("Hi!")
        )
    )
    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "x"}],
            "stream": False,
            "session_id": "s-pinned",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert response.headers["X-Anila-Session-Id"] == "s-pinned"


@respx.mock
def test_router_accepts_anila_session_id_alias(db_path: Path) -> None:
    respx.post(CSP_URL).mock(
        return_value=httpx.Response(
            200, json=_llm_router_response("Hi!")
        )
    )
    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "x"}],
            "stream": False,
            "anila_session_id": "s-alias",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert response.headers["X-Anila-Session-Id"] == "s-alias"


# ---------------------------------------------------------------------------
# Persistence — user message + state endpoint
# ---------------------------------------------------------------------------


@respx.mock
def test_state_endpoint_returns_persisted_user_message(
    db_path: Path,
) -> None:
    respx.post(CSP_URL).mock(
        return_value=httpx.Response(
            200, json=_llm_router_response("Hi back")
        )
    )
    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)

    client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "remember me"}],
            "stream": False,
            "session_id": "s-state",
        },
        headers={"Authorization": "Bearer sk-test"},
    )

    state = client.get("/v1/sessions/s-state/state").json()
    assert state["session_id"] == "s-state"
    assert any(
        m.get("content") == "remember me"
        or (
            isinstance(m.get("content"), list)
            and any("remember me" in str(b.get("text", "")) for b in m["content"])
        )
        for m in state["messages"]
    )


# ---------------------------------------------------------------------------
# Dispatch carries session_id as anila_session_id extension field
# ---------------------------------------------------------------------------


@respx.mock
def test_dispatch_forwards_session_id_to_target_agent(
    db_path: Path,
) -> None:
    """When Router LLM emits DISPATCH, the call to CSP must carry our
    extension field so the target agent can attach the same Session."""

    captured_payloads: list[dict] = []

    def csp_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        captured_payloads.append(body)
        # Differentiate router-LLM call vs agent-dispatch call:
        # router-LLM → model is the configured router model
        # agent-dispatch → model is the agent_id
        if body["model"] == "agent-asrd":
            return httpx.Response(
                200, json=_llm_router_response("agent answer here")
            )
        return httpx.Response(
            200, json=_llm_router_response("DISPATCH:agent-asrd:tell me about X")
        )

    respx.post(CSP_URL).mock(side_effect=csp_handler)

    # Router needs the agent registry to know "agent-asrd" exists.
    # Mock the registry refresh so the agent shows up.
    respx.get(CSP_AGENTS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "agent-asrd",
                        "name": "ASRD",
                        "description_for_router": "Drone specs",
                        "endpoint_url": "http://agent-asrd",
                        "requires_encryption": False,
                    }
                ]
            },
        )
    )

    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)
    client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "tell me about X"}],
            "stream": False,
            "session_id": "s-dispatch",
        },
        headers={"Authorization": "Bearer sk-test"},
    )

    # Find the agent-dispatch payload (model = agent_id)
    agent_dispatches = [
        p for p in captured_payloads if p.get("model") == "agent-asrd"
    ]
    assert len(agent_dispatches) >= 1
    assert agent_dispatches[0]["anila_session_id"] == "s-dispatch"


# ---------------------------------------------------------------------------
# session_factory override
# ---------------------------------------------------------------------------


@respx.mock
def test_session_factory_override_used() -> None:
    respx.post(CSP_URL).mock(
        return_value=httpx.Response(
            200, json=_llm_router_response("hi!")
        )
    )

    captured: dict[str, MemorySession] = {}

    def factory(sid: str) -> MemorySession:
        sess = MemorySession(sid)
        captured[sid] = sess
        return sess

    app = create_router_app(session_factory=factory)
    client = TestClient(app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "session_id": "s-mem",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert response.status_code == 200
    assert "s-mem" in captured
