"""Tests for Sprint 13 PR A2 — Router resume-proxy + ownership pinning.

Covers:

  * dispatching a query persists ``session_id → agent_id`` to the
    Router's SQLite ``session_owners`` table
  * ``GET /v1/sessions/{id}/state`` surfaces the recorded owner
  * ``POST /v1/sessions/{id}/answer`` 404s when no owner is recorded
  * ``POST /v1/sessions/{id}/answer`` proxies through CSP's new
    ``/v1/agents/{agent}/sessions/{id}/answer`` endpoint and the
    response carries the agent's SSE pass-through
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest_asyncio
import respx
from fastapi.testclient import TestClient

from anila_core.api.router_server import create_router_app
from anila_core.api.session_owner import get_session_owner
from anila_core.config import settings
from anila_core.memory import close_all_connections


CSP_BASE = settings.csp_base_url
CSP_URL = f"{CSP_BASE}/v1/chat/completions"
CSP_AGENTS_URL = f"{CSP_BASE}/v1/agents"


@pytest_asyncio.fixture
async def db_path(tmp_path: Path):
    db = tmp_path / "router-resume.db"
    yield db
    await close_all_connections()


def _llm_router_response(content: str) -> dict:
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


def _agent_registry_response(agent_id: str) -> dict:
    return {
        "data": [
            {
                "id": agent_id,
                "name": agent_id,
                "description_for_router": "Demo agent",
                "endpoint_url": f"http://{agent_id}",
                "requires_encryption": False,
            }
        ]
    }


# ---------------------------------------------------------------------------
# Pin owner on dispatch
# ---------------------------------------------------------------------------


@respx.mock
def test_dispatch_pins_owning_agent(db_path: Path) -> None:
    """A successful dispatch writes (session_id, agent_id) to
    session_owners so the resume endpoint can find it later."""

    def csp_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body["model"] == "agent-pinme":
            return httpx.Response(
                200, json=_llm_router_response("agent reply")
            )
        return httpx.Response(
            200,
            json=_llm_router_response("DISPATCH:agent-pinme:do thing"),
        )

    respx.post(CSP_URL).mock(side_effect=csp_handler)
    respx.get(CSP_AGENTS_URL).mock(
        return_value=httpx.Response(
            200, json=_agent_registry_response("agent-pinme")
        )
    )

    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)
    client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "do thing"}],
            "stream": False,
            "session_id": "s-pin",
        },
        headers={"Authorization": "Bearer sk-test"},
    )

    import asyncio
    owner = asyncio.get_event_loop().run_until_complete(
        get_session_owner(str(Path(db_path).resolve()), "s-pin")
    )
    assert owner == "agent-pinme"


@respx.mock
def test_state_endpoint_surfaces_owning_agent(db_path: Path) -> None:
    """The state response carries owner_agent_id post-dispatch."""

    def csp_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body["model"] == "agent-state":
            return httpx.Response(
                200, json=_llm_router_response("ok")
            )
        return httpx.Response(
            200,
            json=_llm_router_response("DISPATCH:agent-state:hi"),
        )

    respx.post(CSP_URL).mock(side_effect=csp_handler)
    respx.get(CSP_AGENTS_URL).mock(
        return_value=httpx.Response(
            200, json=_agent_registry_response("agent-state")
        )
    )

    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)
    client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "session_id": "s-state-2",
        },
        headers={"Authorization": "Bearer sk-test"},
    )

    state = client.get("/v1/sessions/s-state-2/state").json()
    assert state["owner_agent_id"] == "agent-state"


# ---------------------------------------------------------------------------
# Resume endpoint
# ---------------------------------------------------------------------------


def test_answer_without_required_fields_400s(db_path: Path) -> None:
    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)
    resp = client.post(
        "/v1/sessions/s-x/answer",
        json={"answer": "yes"},  # missing interrupt_id
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resp.status_code == 400
    assert "interrupt_id" in resp.text


def test_answer_unknown_session_404s(db_path: Path) -> None:
    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)
    resp = client.post(
        "/v1/sessions/s-unknown/answer",
        json={"interrupt_id": "i-1", "answer": "yes"},
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resp.status_code == 404
    assert "owning agent" in resp.text


def test_answer_with_session_factory_returns_503(db_path: Path) -> None:
    """Custom session_factory paths skip the production owners table; the
    resume proxy must explicitly tell the caller it's unsupported."""
    from anila_core.memory import MemorySession

    app = create_router_app(session_factory=lambda sid: MemorySession(sid))
    client = TestClient(app)
    resp = client.post(
        "/v1/sessions/s-x/answer",
        json={"interrupt_id": "i-1", "answer": "yes"},
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resp.status_code == 503
    assert "session_factory" in resp.text


@respx.mock
def test_answer_proxies_to_csp_resume_endpoint(db_path: Path) -> None:
    """Owner is pinned via dispatch, then a follow-up answer should
    POST to CSP's /v1/agents/{agent}/sessions/{id}/answer."""

    # 1) First, dispatch to pin owner = "agent-resume".
    def csp_chat_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body["model"] == "agent-resume":
            return httpx.Response(
                200, json=_llm_router_response("ok")
            )
        return httpx.Response(
            200,
            json=_llm_router_response("DISPATCH:agent-resume:hi"),
        )

    respx.post(CSP_URL).mock(side_effect=csp_chat_handler)
    respx.get(CSP_AGENTS_URL).mock(
        return_value=httpx.Response(
            200, json=_agent_registry_response("agent-resume")
        )
    )

    # 2) Mock the new CSP resume endpoint to stream a tiny SSE body.
    csp_resume_url = (
        f"{CSP_BASE}/v1/agents/agent-resume/sessions/s-resume/answer"
    )
    captured_resume_bodies: list[dict] = []

    def csp_resume_handler(request: httpx.Request) -> httpx.Response:
        captured_resume_bodies.append(json.loads(request.content.decode()))
        sse_body = (
            "event: anila.resumed\n"
            'data: {"interrupt_id":"i-7"}\n\n'
            'data: {"choices":[{"delta":{"content":"resumed text"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            content=sse_body.encode("utf-8"),
            headers={"Content-Type": "text/event-stream"},
        )

    respx.post(csp_resume_url).mock(side_effect=csp_resume_handler)

    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)

    # Pin owner.
    client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "session_id": "s-resume",
        },
        headers={"Authorization": "Bearer sk-test"},
    )

    # Resume.
    resume_resp = client.post(
        "/v1/sessions/s-resume/answer",
        json={"interrupt_id": "i-7", "answer": "go ahead"},
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resume_resp.status_code == 200
    assert resume_resp.headers.get("X-Anila-Owner-Agent") == "agent-resume"
    assert resume_resp.headers.get("X-Anila-Session-Id") == "s-resume"

    body = resume_resp.text
    # Router emits its own anila.resumed marker first, then the agent's
    # SSE body passes through verbatim (which itself contains another
    # anila.resumed from the agent — both are valid).
    assert "event: anila.resumed" in body
    assert "resumed text" in body

    # CSP saw the user's payload verbatim.
    assert len(captured_resume_bodies) == 1
    assert captured_resume_bodies[0] == {
        "interrupt_id": "i-7",
        "answer": "go ahead",
    }
