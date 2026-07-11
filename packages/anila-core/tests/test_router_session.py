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

from anila_core.api.router_server import (
    ACCESS_COOKIE_NAME,
    _access_cookie_name,
    create_router_app,
)
from anila_core.config import settings
from anila_core.memory import MemorySession, close_all_connections


CSP_BASE = settings.csp_base_url
CSP_URL = f"{CSP_BASE}/v1/chat/completions"
CSP_AGENTS_URL = f"{CSP_BASE}/v1/agents"
CSP_ME_URL = f"{CSP_BASE}/api/auth/me"


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


def _jwt(label: str) -> str:
    return f"jwt-header.{label}.jwt-signature"


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

    state_response = client.get(
        "/v1/sessions/s-state/state",
        headers={"Authorization": "Bearer sk-test"},
    )
    assert state_response.status_code == 200, state_response.text
    state = state_response.json()
    assert state["session_id"] == "s-state"
    assert any(
        m.get("content") == "remember me"
        or (
            isinstance(m.get("content"), list)
            and any("remember me" in str(b.get("text", "")) for b in m["content"])
        )
        for m in state["messages"]
    )


@respx.mock
def test_state_endpoint_requires_auth(db_path: Path) -> None:
    respx.post(CSP_URL).mock(
        return_value=httpx.Response(
            200, json=_llm_router_response("private")
        )
    )
    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "secret"}],
            "stream": False,
            "session_id": "s-private",
        },
        headers={"Authorization": "Bearer sk-owner"},
    )
    assert response.status_code == 200

    state = client.get("/v1/sessions/s-private/state")
    assert state.status_code == 401


@respx.mock
def test_router_accepts_only_the_configured_host_cookie(db_path: Path) -> None:
    assert ACCESS_COOKIE_NAME == "__Host-anila_access_token"
    assert _access_cookie_name(False) == "anila_dev_access_token"
    respx.post(CSP_URL).mock(
        return_value=httpx.Response(200, json=_llm_router_response("cookie ok"))
    )
    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)

    accepted = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "x"}], "stream": False},
        headers={"Cookie": f"{ACCESS_COOKIE_NAME}=sk-cookie-user"},
    )
    assert accepted.status_code == 200, accepted.text

    rejected = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "x"}], "stream": False},
        headers={"Cookie": "anila_access_token=sk-attacker-shadow"},
    )
    assert rejected.status_code == 401


@respx.mock
def test_state_endpoint_rejects_different_caller(db_path: Path) -> None:
    respx.post(CSP_URL).mock(
        return_value=httpx.Response(
            200, json=_llm_router_response("private")
        )
    )
    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "owner-only"}],
            "stream": False,
            "session_id": "s-owned",
        },
        headers={"Authorization": "Bearer sk-owner"},
    )
    assert response.status_code == 200

    state = client.get(
        "/v1/sessions/s-owned/state",
        headers={"Authorization": "Bearer sk-other"},
    )
    assert state.status_code == 403


@respx.mock
def test_state_endpoint_accepts_refreshed_jwt_for_same_user(
    db_path: Path,
) -> None:
    """Cookie/JWT auth refreshes the bearer string; ownership is the CSP user."""
    respx.post(CSP_URL).mock(
        return_value=httpx.Response(
            200, json=_llm_router_response("Hi back")
        )
    )

    seen_tokens: list[str] = []

    def me_handler(request: httpx.Request) -> httpx.Response:
        seen_tokens.append(request.headers["Authorization"])
        return httpx.Response(
            200,
            json={
                "id": 42,
                "username": "rotating-user",
                "email": None,
                "role": "user",
                "department_id": None,
                "department_name": None,
                "is_active": True,
                "is_approved": True,
                "local_password_disabled": False,
                "last_login_at": None,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            },
        )

    respx.get(CSP_ME_URL).mock(side_effect=me_handler)

    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)

    first = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "remember me"}],
            "stream": False,
            "session_id": "s-jwt-rotate",
        },
        headers={"Authorization": f"Bearer {_jwt('access-v1')}"},
    )
    assert first.status_code == 200, first.text

    state = client.get(
        "/v1/sessions/s-jwt-rotate/state",
        headers={"Authorization": f"Bearer {_jwt('access-v2')}"},
    )
    assert state.status_code == 200, state.text
    assert seen_tokens == [
        f"Bearer {_jwt('access-v1')}",
        f"Bearer {_jwt('access-v2')}",
    ]


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


@respx.mock
def test_session_factory_jwt_chat_does_not_resolve_owner_hash(monkeypatch) -> None:
    """Custom session adapters do not use Router's SQLite owner table.

    JWT chat turns in this mode must not call CSP /api/auth/me through
    ``_resolve_session_owner_hash`` because the result is unused.
    """
    import anila_core.api.router_server as router_server

    async def fail_if_called(_caller_api_key: str) -> str:
        raise AssertionError("owner hash resolution should not run")

    monkeypatch.setattr(
        router_server, "_resolve_session_owner_hash", fail_if_called
    )
    respx.post(CSP_URL).mock(
        return_value=httpx.Response(
            200, json=_llm_router_response("hi!")
        )
    )

    app = create_router_app(session_factory=lambda sid: MemorySession(sid))
    client = TestClient(app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "session_id": "s-custom-jwt",
        },
        headers={"Authorization": f"Bearer {_jwt('access-v1')}"},
    )

    assert response.status_code == 200, response.text


def test_session_factory_state_does_not_resolve_owner_hash(monkeypatch) -> None:
    """State reads with a custom session adapter should not depend on CSP
    auth-me because there is no local owner hash to compare.
    """
    import anila_core.api.router_server as router_server

    async def fail_if_called(_caller_api_key: str) -> str:
        raise AssertionError("owner hash resolution should not run")

    monkeypatch.setattr(
        router_server, "_resolve_session_owner_hash", fail_if_called
    )

    app = create_router_app(session_factory=lambda sid: MemorySession(sid))
    client = TestClient(app)
    response = client.get(
        "/v1/sessions/s-custom-jwt/state",
        headers={"Authorization": f"Bearer {_jwt('access-v1')}"},
    )

    assert response.status_code == 200, response.text
