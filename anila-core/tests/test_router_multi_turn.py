"""Tests for Sprint 10 PR 4 — Router multi-turn orchestration.

Verifies:

- Default behaviour (no ``anila_multi_turn``) still single-shot
- ``anila_multi_turn=2`` lets Router LLM produce a final synthesis after
  the first dispatch
- ``anila_multi_turn=3`` allows a second DISPATCH after seeing the first
  agent's reply
- Loop bounded by max_iterations
- Trace records each iteration
"""

from __future__ import annotations

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
    db = tmp_path / "router-multi.db"
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


def _make_csp_handler(scripted_responses: list[dict | callable]):
    """Build a respx handler that scripts a sequence of CSP replies.

    Each entry can be a dict (returned as JSON) or a callable
    ``(request) -> httpx.Response``.
    """
    iterator = iter(scripted_responses)

    def handler(request: httpx.Request) -> httpx.Response:
        try:
            nxt = next(iterator)
        except StopIteration:
            return httpx.Response(500, json={"detail": "script exhausted"})
        if callable(nxt):
            return nxt(request)
        return httpx.Response(200, json=nxt)

    return handler


# ---------------------------------------------------------------------------
# Default — no multi-turn
# ---------------------------------------------------------------------------


@respx.mock
def test_default_behaviour_unchanged_without_flag(db_path: Path) -> None:
    """anila_multi_turn omitted → single dispatch like before."""
    respx.get(CSP_AGENTS_URL).mock(
        return_value=httpx.Response(200, json=_agent_list_response())
    )
    csp = respx.post(CSP_URL).mock(
        side_effect=_make_csp_handler(
            [
                _completion("DISPATCH:agent-a:answer please"),  # router decides
                _completion("hello from A", model="agent-a"),  # agent A reply
            ]
        )
    )
    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "go"}],
            "stream": False,
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "hello from A"
    assert csp.call_count == 2  # 1 router + 1 agent dispatch


# ---------------------------------------------------------------------------
# anila_multi_turn=2 — Router synthesises after first dispatch
# ---------------------------------------------------------------------------


@respx.mock
def test_router_synthesises_after_first_dispatch_when_multi_turn_enabled(
    db_path: Path,
) -> None:
    respx.get(CSP_AGENTS_URL).mock(
        return_value=httpx.Response(200, json=_agent_list_response())
    )
    csp = respx.post(CSP_URL).mock(
        side_effect=_make_csp_handler(
            [
                _completion("DISPATCH:agent-a:tell me X"),  # router round 1
                _completion("X is 42", model="agent-a"),  # agent A
                _completion(
                    "Final answer: A reported X = 42, which is the canonical value."
                ),  # router round 2 — direct answer
            ]
        )
    )
    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "what's X?"}],
            "stream": False,
            "anila_multi_turn": 2,
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert response.status_code == 200
    body = response.json()
    text = body["choices"][0]["message"]["content"]
    assert "Final answer" in text
    assert "42" in text
    assert csp.call_count == 3  # round1 router + agent + round2 router


# ---------------------------------------------------------------------------
# anila_multi_turn=3 — Router dispatches twice
# ---------------------------------------------------------------------------


@respx.mock
def test_router_can_dispatch_a_second_agent_in_one_turn(
    db_path: Path,
) -> None:
    respx.get(CSP_AGENTS_URL).mock(
        return_value=httpx.Response(200, json=_agent_list_response())
    )
    csp = respx.post(CSP_URL).mock(
        side_effect=_make_csp_handler(
            [
                _completion("DISPATCH:agent-a:fetch data X"),  # router round 1
                _completion("data X = 42", model="agent-a"),  # agent A
                _completion(
                    "DISPATCH:agent-b:given X=42, what does that mean?"
                ),  # router round 2
                _completion(
                    "X=42 means the meaning of life", model="agent-b"
                ),  # agent B
                _completion(
                    "Combined answer: A says X=42, B says it's the meaning of life."
                ),  # router round 3 — synthesis
            ]
        )
    )
    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {"role": "user", "content": "explain X"}
            ],
            "stream": False,
            "anila_multi_turn": 3,
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert response.status_code == 200
    body = response.json()
    text = body["choices"][0]["message"]["content"]
    assert "Combined answer" in text
    assert csp.call_count == 5  # 3 router + 2 agent
    # The trace should record dispatches for both agents.
    trace = body.get("anila_meta", {}).get("trace") or []
    dispatched_ids = [
        s["detail"]
        for s in trace
        if s.get("kind") == "dispatch"
    ]
    assert any("agent-a" in d for d in dispatched_ids)
    assert any("agent-b" in d for d in dispatched_ids)


# ---------------------------------------------------------------------------
# Loop bounded by max_iterations
# ---------------------------------------------------------------------------


@respx.mock
def test_loop_caps_at_max_iterations(db_path: Path) -> None:
    """If LLM keeps DISPATCHing, we still stop after N iterations."""
    respx.get(CSP_AGENTS_URL).mock(
        return_value=httpx.Response(200, json=_agent_list_response())
    )
    # Even if the router keeps dispatching, max_iterations=2 means only
    # 2 total router LLM calls → exactly 1 dispatch (the first one),
    # then the second LLM call would produce another DISPATCH but we
    # exit the loop and return the last agent reply.
    csp = respx.post(CSP_URL).mock(
        side_effect=_make_csp_handler(
            [
                _completion("DISPATCH:agent-a:q1"),  # router round 1
                _completion("agent A reply 1", model="agent-a"),  # agent A
                _completion("DISPATCH:agent-b:q2"),  # router round 2
                _completion("agent B reply 2", model="agent-b"),  # agent B
            ]
        )
    )
    app = create_router_app(session_db_path=str(db_path))
    client = TestClient(app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "go"}],
            "stream": False,
            "anila_multi_turn": 2,
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert response.status_code == 200
    body = response.json()
    # Last agent reply at iteration 2 was agent B → that's the response.
    text = body["choices"][0]["message"]["content"]
    assert text == "agent B reply 2"
    assert csp.call_count == 4
