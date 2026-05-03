"""Sprint 13 PR D1 — end-to-end ask_user → resume happy-path test.

Wires up:

  * Router (real ``create_router_app``)
  * Mocked CSP that proxies between Router and the (mocked) agent
  * Mocked agent that on the first turn emits an ``interrupt_requested``
    event with kind=ask_user, then on resume answers the user

Verifies:

  * dispatch pins (session_id, agent_id) so resume can find the agent
  * Router forwards the agent's named SSE events (``anila.trace`` /
    ``anila.interrupt_requested``) to the user
  * ``POST /v1/sessions/{sid}/answer`` reaches CSP's resume endpoint
    with the user's payload intact and streams the agent's response
    back through both proxies
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
CSP_CHAT_URL = f"{CSP_BASE}/v1/chat/completions"
CSP_AGENTS_URL = f"{CSP_BASE}/v1/agents"


@pytest_asyncio.fixture
async def db_path(tmp_path: Path):
    db = tmp_path / "e2e-resume.db"
    yield db
    await close_all_connections()


def _llm_router_dispatch(agent_id: str, query: str) -> dict:
    """LLM response that triggers DISPATCH:<agent>:<query>."""
    return {
        "id": "chatcmpl-r",
        "object": "chat.completion",
        "created": 0,
        "model": "router-llm",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": f"DISPATCH:{agent_id}:{query}",
                },
                "finish_reason": "stop",
            }
        ],
    }


def _agent_sse_emits_interrupt(interrupt_id: str) -> str:
    """Agent's first-turn SSE: empty content + ask_user interrupt."""
    interrupt_payload = {
        "interrupt_id": interrupt_id,
        "kind": "ask_user",
        "payload": {
            "question": "Pick which file to read",
            "options": ["a.txt", "b.txt"],
            "multi_select": False,
            "allow_other": False,
        },
    }
    trace_step = {
        "kind": "agent",
        "label": "Agent thinking",
        "detail": "needs user input",
        "status": "ok",
    }
    return (
        f"event: anila.trace\ndata: {json.dumps(trace_step)}\n\n"
        f"event: interrupt_requested\n"
        f"data: {json.dumps(interrupt_payload)}\n\n"
        "data: [DONE]\n\n"
    )


def _agent_sse_resumed_answer() -> str:
    """Agent's resume-turn SSE: answers based on user's pick."""
    return (
        'event: anila.trace\ndata: {"kind":"agent","label":"resumed","detail":"got answer","status":"ok"}\n\n'
        'data: {"choices":[{"delta":{"content":"Reading a.txt → contents are: hello world"}}]}\n\n'
        "data: [DONE]\n\n"
    )


@respx.mock
def test_ask_user_then_resume_end_to_end(db_path: Path) -> None:
    """The full Router → CSP → agent loop for an ask_user interrupt + resume."""

    # CSP /v1/agents — Router needs to know agent-demo exists.
    respx.get(CSP_AGENTS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "agent-demo",
                        "name": "agent-demo",
                        "description_for_router": "demo agent",
                        "endpoint_url": "http://agent-demo",
                        "requires_encryption": False,
                    }
                ]
            },
        )
    )

    # CSP /v1/chat/completions handles two roles:
    #   1. Router-LLM call (model != "agent-demo") → return DISPATCH
    #      directive. The Router's single-shot streaming path uses
    #      ``_stream_llm_sse`` so we must respond as SSE here too;
    #      non-streaming JSON would fall back to "(LLM 暫時無法回應)".
    #   2. Agent dispatch (model == "agent-demo", stream=True) → return
    #      the SSE that emits the interrupt.
    def _router_llm_sse() -> str:
        chunk = {
            "id": "chatcmpl-r",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "router-llm",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "DISPATCH:agent-demo:do something"},
                    "finish_reason": None,
                }
            ],
        }
        stop = {
            **chunk,
            "choices": [
                {"index": 0, "delta": {}, "finish_reason": "stop"}
            ],
        }
        return (
            f"data: {json.dumps(chunk)}\n\n"
            f"data: {json.dumps(stop)}\n\n"
            "data: [DONE]\n\n"
        )

    def csp_chat_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("model") == "agent-demo":
            sse_body = _agent_sse_emits_interrupt(interrupt_id="int-7")
            return httpx.Response(
                200,
                content=sse_body.encode("utf-8"),
                headers={"Content-Type": "text/event-stream"},
            )
        # Router-LLM call.
        return httpx.Response(
            200,
            content=_router_llm_sse().encode("utf-8"),
            headers={"Content-Type": "text/event-stream"},
        )

    respx.post(CSP_CHAT_URL).mock(side_effect=csp_chat_handler)

    # CSP resume proxy — Router's POST /v1/sessions/{sid}/answer hits this.
    captured_resume_bodies: list[dict] = []
    csp_resume_url = (
        f"{CSP_BASE}/v1/agents/agent-demo/sessions/sid-e2e/answer"
    )

    def csp_resume_handler(request: httpx.Request) -> httpx.Response:
        captured_resume_bodies.append(json.loads(request.content.decode()))
        return httpx.Response(
            200,
            content=_agent_sse_resumed_answer().encode("utf-8"),
            headers={"Content-Type": "text/event-stream"},
        )

    respx.post(csp_resume_url).mock(side_effect=csp_resume_handler)

    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)

    # ── Step 1 — initial streaming turn that lands on the interrupt ──
    first_resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "do something"}],
            "stream": True,
            "session_id": "sid-e2e",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert first_resp.status_code == 200
    body = first_resp.text

    # Router renamed agent's typed event into the anila.* namespace.
    assert "event: anila.interrupt_requested" in body
    assert "int-7" in body
    assert "ask_user" in body
    assert "Pick which file to read" in body

    # session_owners pinned to the dispatched agent.
    state = client.get("/v1/sessions/sid-e2e/state").json()
    assert state["owner_agent_id"] == "agent-demo"
    # Pending interrupts surface from the Router's session state too.
    # (Note: the Router-side Session doesn't see the agent's interrupt
    # writes — those live in the agent's own SqliteSession. The
    # ``pending_interrupts`` field here will be empty; the UI relies on
    # the SSE event for live state, not on this field.)

    # ── Step 2 — user answers the interrupt → Router forwards ──
    resume_resp = client.post(
        "/v1/sessions/sid-e2e/answer",
        json={"interrupt_id": "int-7", "answer": "a.txt"},
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resume_resp.status_code == 200

    # The Router emits its own anila.resumed before the agent reply,
    # then passes the agent's stream through verbatim.
    resume_body = resume_resp.text
    assert "event: anila.resumed" in resume_body
    assert "Reading a.txt" in resume_body
    # Owner agent surfaced as a header so the UI can show "Resume on X".
    assert resume_resp.headers.get("X-Anila-Owner-Agent") == "agent-demo"

    # CSP saw the user's payload unchanged.
    assert len(captured_resume_bodies) == 1
    assert captured_resume_bodies[0] == {
        "interrupt_id": "int-7",
        "answer": "a.txt",
    }
