from __future__ import annotations

from fastapi.testclient import TestClient

from anila_core.api import router_server
from anila_core.registry.remote_agent_manifest import RemoteAgentManifest, RemoteAgentRegistry


def test_router_non_stream_includes_anila_meta(monkeypatch):
    app = router_server.create_router_app()
    client = TestClient(app)

    async def fake_ensure_fresh(self, api_key: str) -> None:
        return None

    def fake_list_agents(self, api_key: str):
        return [
            RemoteAgentManifest(
                agent_id="hr-policy",
                name="HR Policy",
                description_for_router="Handle leave policy questions",
                endpoint_url="http://agent:9100",
            )
        ]

    def fake_get(self, api_key: str, agent_id: str):
        return fake_list_agents(self, api_key)[0]

    async def fake_call_llm(api_key: str, messages: list[dict]):
        return {"content": "DISPATCH:hr-policy:幫我查特休規則", "anila_meta": None, "raw": None}

    async def fake_dispatch(**kwargs):
        return {
            "content": "特休規則如下",
            "anila_meta": {
                "trace_id": "trace-agent",
                "trace": [{"kind": "agent", "label": "HR 查詢", "detail": "查詢特休規則", "status": "ok"}],
                "citations": [],
                "confidence": {"level": "high", "score": 0.9, "reasons": ["policy_match"]},
                "handoff_chain": [],
                "follow_ups": ["還要查半天假嗎？"],
                "latency_ms": 12,
                "classified": False,
            },
            "raw": None,
        }

    monkeypatch.setattr(RemoteAgentRegistry, "ensure_fresh", fake_ensure_fresh)
    monkeypatch.setattr(RemoteAgentRegistry, "list_agents", fake_list_agents)
    monkeypatch.setattr(RemoteAgentRegistry, "get", fake_get)
    monkeypatch.setattr(router_server, "_call_llm_non_stream", fake_call_llm)
    monkeypatch.setattr(router_server, "dispatch_to_agent_response", fake_dispatch)

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test"},
        json={
            "model": "anila-router",
            "messages": [{"role": "user", "content": "幫我查特休規則"}],
            "stream": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["choices"][0]["message"]["content"] == "特休規則如下"
    assert payload["anila_meta"]["trace"]
    assert payload["anila_meta"]["handoff_chain"][0]["agent_id"] == "anila-router"
    assert payload["anila_meta"]["follow_ups"] == ["還要查半天假嗎？"]


def test_router_stream_emits_trace_and_meta_events(monkeypatch):
    app = router_server.create_router_app()
    client = TestClient(app)

    async def fake_ensure_fresh(self, api_key: str) -> None:
        return None

    def fake_list_agents(self, api_key: str):
        return []

    def fake_get(self, api_key: str, agent_id: str):
        return None

    async def fake_call_llm(api_key: str, messages: list[dict]):
        return {"content": "直接回答內容", "anila_meta": None, "raw": None}

    monkeypatch.setattr(RemoteAgentRegistry, "ensure_fresh", fake_ensure_fresh)
    monkeypatch.setattr(RemoteAgentRegistry, "list_agents", fake_list_agents)
    monkeypatch.setattr(RemoteAgentRegistry, "get", fake_get)
    monkeypatch.setattr(router_server, "_call_llm_non_stream", fake_call_llm)

    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test"},
        json={
            "model": "anila-router",
            "messages": [{"role": "user", "content": "你好"}],
            "stream": True,
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert "event: anila.trace" in body
    assert "event: anila.meta" in body
    assert "直接回答內容" in body
    assert "data: [DONE]" in body
