"""Router dispatch hygiene — the five backend fixes behind "automatic dispatch
has never once run, yet every message pays for it".

FIX 1  zero agents → plain assistant prompt, no routing rules at all
FIX 2  a caller-supplied system message no longer disables dispatch
FIX 3  prose / reasoning that merely mentions the syntax must not dispatch
FIX 5  the governance UI's ``is_router_primary`` is actually honoured
FIX 6  the dispatched agent is a first-class meta field on BOTH paths
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import respx
from fastapi.testclient import TestClient

import anila_core.api.router_server as rs
from anila_core.api.router_server import create_router_app
from anila_core.config import settings
from anila_core.memory import close_all_connections
from anila_core.registry.remote_agent_manifest import (
    RemoteAgentManifest,
    RemoteAgentRegistry,
)


CSP_BASE = settings.csp_base_url
CSP_URL = f"{CSP_BASE}/v1/chat/completions"
CSP_AGENTS_URL = f"{CSP_BASE}/v1/agents"
CSP_ROUTER_PRIMARY_URL = f"{CSP_BASE}/api/models/router-primary"


@pytest.fixture(autouse=True)
def _disable_recompose(monkeypatch):
    """Recompose is an orthogonal personalization pass with its own tests."""

    async def _passthrough(agent_reply, caller_api_key, *, forwarded_headers=None):
        return agent_reply, "skipped"

    monkeypatch.setattr(rs, "_recompose_reply", _passthrough)


@pytest_asyncio.fixture
async def db_path(tmp_path: Path):
    db = tmp_path / "router-hygiene.db"
    yield db
    await close_all_connections()


def _manifest(agent_id: str = "agent-a") -> RemoteAgentManifest:
    return RemoteAgentManifest(
        agent_id=agent_id,
        name=agent_id,
        description_for_router=f"Specialist {agent_id}",
        endpoint_url=f"http://{agent_id}",
    )


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


def _agents_payload(*manifests: RemoteAgentManifest) -> dict:
    return {
        "data": [
            {
                "id": m.agent_id,
                "name": m.name,
                "description_for_router": m.description_for_router,
                "endpoint_url": m.endpoint_url,
                "requires_encryption": False,
            }
            for m in manifests
        ]
    }


def _scripted(responses):
    iterator = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        try:
            nxt = next(iterator)
        except StopIteration:  # pragma: no cover — a script bug, not a fixture
            return httpx.Response(500, json={"detail": "script exhausted"})
        if callable(nxt):
            return nxt(request)
        return httpx.Response(200, json=nxt)

    return handler


def _install_fake_llm(monkeypatch, replies, captured):
    """Replace the routing LLM call; record the messages it was handed."""
    it = iter(replies)

    async def fake(api_key, messages, *, forwarded_headers=None):
        captured.append(messages)
        return {
            "content": next(it),
            "reasoning": None,
            "anila_meta": None,
            "raw": None,
            "error": None,
        }

    monkeypatch.setattr(rs, "_call_llm_non_stream", fake)


def _install_registry(monkeypatch, agent_lists):
    """Serve a *different* agent list on each request (FIX 1 liveness)."""
    it = iter(agent_lists)
    state = {"current": []}

    async def fake_ensure_fresh(self, api_key: str) -> None:
        state["current"] = next(it)

    def fake_list_agents(self, api_key: str):
        return state["current"]

    def fake_get(self, api_key: str, agent_id: str):
        for m in state["current"]:
            if m.agent_id == agent_id:
                return m
        return None

    monkeypatch.setattr(RemoteAgentRegistry, "ensure_fresh", fake_ensure_fresh)
    monkeypatch.setattr(RemoteAgentRegistry, "list_agents", fake_list_agents)
    monkeypatch.setattr(RemoteAgentRegistry, "get", fake_get)


def _post(client: TestClient, messages, **extra):
    return client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test"},
        json={"messages": messages, "stream": False, **extra},
    )


def _system_of(messages) -> str:
    return "\n".join(
        m["content"] for m in messages if m.get("role") == "system"
    )


# ---------------------------------------------------------------------------
# FIX 1 — no agents, no routing prompt
# ---------------------------------------------------------------------------


def test_zero_agents_prompt_has_no_dispatch_instructions(db_path, monkeypatch):
    captured: list = []
    _install_registry(monkeypatch, [[]])
    _install_fake_llm(monkeypatch, ["你好"], captured)

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    assert _post(client, [{"role": "user", "content": "嗨"}]).status_code == 200

    system = _system_of(captured[0])
    assert "DISPATCH" not in system
    assert "Available agents" not in system
    # …but the platform's own voice survives: no leaked analysis, truthful
    # "no agents" answer, personalization.
    assert "PERSONALIZATION" in system
    assert "目前沒有已註冊的 agent" in system


def test_registering_an_agent_routes_on_the_very_next_request(db_path, monkeypatch):
    """The empty-prompt choice is per-request, not per-boot."""
    captured: list = []
    # request 1 sees no agents, request 2 sees one — same app, no restart.
    _install_registry(monkeypatch, [[], [_manifest()]])
    _install_fake_llm(monkeypatch, ["你好", "DISPATCH:agent-a:查一下"], captured)

    async def fake_dispatch(**kwargs):
        return {"content": "agent answered", "anila_meta": None, "raw": None}

    monkeypatch.setattr(rs, "dispatch_to_agent_response", fake_dispatch)

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    _post(client, [{"role": "user", "content": "嗨"}])
    second = _post(client, [{"role": "user", "content": "查一下"}])

    assert "DISPATCH" not in _system_of(captured[0])
    routed = _system_of(captured[1])
    assert "DISPATCH" in routed
    assert "agent-a" in routed
    assert second.json()["choices"][0]["message"]["content"] == "agent answered"


# ---------------------------------------------------------------------------
# FIX 2 — a caller system message must not disable dispatch
# ---------------------------------------------------------------------------


def test_caller_system_message_still_gets_routing_instructions(db_path, monkeypatch):
    captured: list = []
    _install_registry(monkeypatch, [[_manifest()]])
    _install_fake_llm(monkeypatch, ["DISPATCH:agent-a:查一下"], captured)

    async def fake_dispatch(**kwargs):
        return {"content": "agent answered", "anila_meta": None, "raw": None}

    monkeypatch.setattr(rs, "dispatch_to_agent_response", fake_dispatch)

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    response = _post(
        client,
        [
            {"role": "system", "content": "You are a pirate."},
            {"role": "user", "content": "查一下"},
        ],
    )

    sent = captured[0]
    # Routing prompt present AND the caller's system message preserved.
    assert "DISPATCH" in sent[0]["content"]
    assert sent[0]["role"] == "system"
    assert {"role": "system", "content": "You are a pirate."} in sent
    # …and dispatch actually happened.
    assert response.json()["choices"][0]["message"]["content"] == "agent answered"


# ---------------------------------------------------------------------------
# FIX 3a — only a line-initial instruction dispatches
# ---------------------------------------------------------------------------


def test_prose_explaining_the_syntax_does_not_dispatch(db_path, monkeypatch):
    captured: list = []
    _install_registry(monkeypatch, [[_manifest()]])
    explainer = "要分派給某個 agent，請寫 DISPATCH:agent-a:你的問題"
    _install_fake_llm(monkeypatch, [explainer], captured)

    async def exploded(**kwargs):  # pragma: no cover — must never run
        raise AssertionError("prose must not dispatch")

    monkeypatch.setattr(rs, "dispatch_to_agent_response", exploded)

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    body = _post(client, [{"role": "user", "content": "怎麼分派？"}]).json()

    assert body["choices"][0]["message"]["content"] == explainer
    assert body["anila_meta"]["route"]["decision"] == "direct"
    assert body["anila_meta"]["answering_agent_id"] is None


def test_a_genuine_instruction_still_dispatches(db_path, monkeypatch):
    captured: list = []
    _install_registry(monkeypatch, [[_manifest()]])
    _install_fake_llm(monkeypatch, ["DISPATCH:agent-a:查一下規則"], captured)

    seen: dict = {}

    async def fake_dispatch(**kwargs):
        seen.update(kwargs)
        return {"content": "agent answered", "anila_meta": None, "raw": None}

    monkeypatch.setattr(rs, "dispatch_to_agent_response", fake_dispatch)

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    body = _post(client, [{"role": "user", "content": "查一下規則"}]).json()

    assert seen["agent_id"] == "agent-a"
    assert seen["query"] == "查一下規則"
    assert body["choices"][0]["message"]["content"] == "agent answered"


def test_backtick_wrapped_instruction_still_dispatches(db_path, monkeypatch):
    """Models wrap the line in backticks; the anchor must tolerate that."""
    captured: list = []
    _install_registry(monkeypatch, [[_manifest()]])
    _install_fake_llm(monkeypatch, ["`DISPATCH:agent-a:查一下規則`"], captured)

    seen: dict = {}

    async def fake_dispatch(**kwargs):
        seen.update(kwargs)
        return {"content": "agent answered", "anila_meta": None, "raw": None}

    monkeypatch.setattr(rs, "dispatch_to_agent_response", fake_dispatch)

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    _post(client, [{"role": "user", "content": "查一下規則"}])
    assert seen["agent_id"] == "agent-a"


# ---------------------------------------------------------------------------
# FIX 3b — the reasoning field is not a dispatch signal
# ---------------------------------------------------------------------------


def test_dispatch_mentioned_only_while_reasoning_does_not_dispatch(db_path, monkeypatch):
    """gpt-oss quotes the routing rules while thinking. Thinking != deciding."""
    _install_registry(monkeypatch, [[_manifest()]])

    async def fake_llm(api_key, messages, *, forwarded_headers=None):
        return {
            "content": "這題我直接回答就好。",
            # The exact shape the old salvage matched: a query-less header.
            "reasoning": "rule 1 says I could emit\nDISPATCH:agent-a:\nbut I won't",
            "anila_meta": None,
            "raw": None,
            "error": None,
        }

    monkeypatch.setattr(rs, "_call_llm_non_stream", fake_llm)

    async def exploded(**kwargs):  # pragma: no cover — must never run
        raise AssertionError("reasoning must not dispatch")

    monkeypatch.setattr(rs, "dispatch_to_agent_response", exploded)

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    body = _post(client, [{"role": "user", "content": "問題"}]).json()

    assert body["choices"][0]["message"]["content"] == "這題我直接回答就好。"
    assert body["anila_meta"]["route"]["decision"] == "direct"


# ---------------------------------------------------------------------------
# FIX 5 — the router honours the governance UI's router-primary model
# ---------------------------------------------------------------------------


@respx.mock
def test_router_uses_the_csp_designated_primary_model(db_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "csp_service_token", "svc-token")
    respx.get(CSP_ROUTER_PRIMARY_URL).mock(
        return_value=httpx.Response(200, json={"id": 3, "name": "gemma26"})
    )
    respx.get(CSP_AGENTS_URL).mock(
        return_value=httpx.Response(200, json=_agents_payload())
    )
    sent: list[dict] = []

    def capture(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=_completion("你好"))

    respx.post(CSP_URL).mock(side_effect=capture)

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    assert _post(client, [{"role": "user", "content": "嗨"}]).status_code == 200
    assert sent[0]["model"] == "gemma26"
    assert client.get("/health").json()["router_model"] == "gemma26"
    assert client.get("/health").json()["router_model_source"] == "csp_registry"


@respx.mock
def test_router_falls_back_to_env_model_when_no_primary_is_set(db_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "csp_service_token", "svc-token")
    respx.get(CSP_ROUTER_PRIMARY_URL).mock(
        return_value=httpx.Response(404, json={"detail": "尚未指定 ANILA 主路由模型"})
    )
    respx.get(CSP_AGENTS_URL).mock(
        return_value=httpx.Response(200, json=_agents_payload())
    )
    sent: list[dict] = []

    def capture(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=_completion("你好"))

    respx.post(CSP_URL).mock(side_effect=capture)

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    _post(client, [{"role": "user", "content": "嗨"}])
    assert sent[0]["model"] == settings.model
    assert client.get("/health").json()["router_model_source"] == "env"


# ---------------------------------------------------------------------------
# FIX 6 — first-class answering_agent_id on both paths
# ---------------------------------------------------------------------------


def test_non_streaming_dispatch_names_the_answering_agent(db_path, monkeypatch):
    captured: list = []
    _install_registry(monkeypatch, [[_manifest()]])
    _install_fake_llm(monkeypatch, ["DISPATCH:agent-a:查一下"], captured)

    async def fake_dispatch(**kwargs):
        return {"content": "agent answered", "anila_meta": None, "raw": None}

    monkeypatch.setattr(rs, "dispatch_to_agent_response", fake_dispatch)

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    meta = _post(client, [{"role": "user", "content": "查一下"}]).json()["anila_meta"]

    assert meta["answering_agent_id"] == "agent-a"
    # the prose form is kept for compatibility
    assert meta["handoff_chain"][0]["output_summary"] == "dispatch to agent-a"


@respx.mock
def test_streaming_dispatch_names_the_answering_agent(db_path) -> None:
    respx.get(CSP_AGENTS_URL).mock(
        return_value=httpx.Response(200, json=_agents_payload(_manifest()))
    )

    def sse(body: str) -> httpx.Response:
        return httpx.Response(
            200,
            content=body.encode("utf-8"),
            headers={"Content-Type": "text/event-stream"},
        )

    respx.post(CSP_URL).mock(
        side_effect=_scripted(
            [
                # 1) the router LLM's streamed routing decision
                lambda _r: sse(
                    'data: {"choices":[{"delta":{"content":"DISPATCH:agent-a:查一下\\n"}}]}\n\n'
                    "data: [DONE]\n\n"
                ),
                # 2) the dispatched agent's streamed answer
                lambda _r: sse(
                    'data: {"choices":[{"delta":{"content":"agent answered"}}]}\n\n'
                    "data: [DONE]\n\n"
                ),
            ]
        )
    )

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test"},
        json={"messages": [{"role": "user", "content": "查一下"}], "stream": True},
    ) as response:
        body = "".join(response.iter_text())

    metas = [
        json.loads(line[len("data: "):])
        for block in body.split("\n\n")
        if block.startswith("event: anila.meta")
        for line in block.split("\n")
        if line.startswith("data: ")
    ]
    assert metas, body
    assert metas[-1]["answering_agent_id"] == "agent-a"
    assert metas[-1]["handoff_chain"][0]["output_summary"] == "dispatch to agent-a"
