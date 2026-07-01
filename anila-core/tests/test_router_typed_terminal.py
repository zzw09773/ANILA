"""Stage 3 — typed-terminal event (`anila.terminal`).

Per the frozen SSE contract (docs/platform/router-sse-contract.md §6, option A),
the Router emits a dedicated `event: anila.terminal` with
``{"reason": completed|max_turns|aborted|budget|length|error, "detail"?: str}``
right before ``[DONE]`` — one per turn — so the ANILA UI can render *why* an
answer stopped without polluting the OpenAI-compat chunk ``finish_reason``.

This first slice covers the helper + the primary direct-answer streaming exit
(reason=completed). error/length/aborted reasons + the remaining exits follow.
"""

from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from anila_core.api import router_server
from anila_core.registry.remote_agent_manifest import RemoteAgentRegistry


def _data_payload(frame: str) -> dict:
    line = next(ln for ln in frame.splitlines() if ln.startswith("data:"))
    return json.loads(line[len("data:"):].strip())


def test_make_terminal_shape():
    """_make_terminal emits a well-formed `event: anila.terminal` SSE frame."""
    frame = router_server._make_terminal("completed")
    assert frame.startswith("event: anila.terminal\n")
    assert frame.endswith("\n\n")
    assert _data_payload(frame) == {"reason": "completed"}

    frame_err = router_server._make_terminal("error", detail="agent down")
    assert _data_payload(frame_err) == {"reason": "error", "detail": "agent down"}


def test_stream_emits_terminal_completed_before_done(monkeypatch):
    """A direct-answer streaming turn emits `event: anila.terminal`
    {reason:completed} immediately before `[DONE]`."""
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
    assert "event: anila.terminal" in body
    assert '"reason": "completed"' in body
    # terminal must precede [DONE]
    assert body.index("event: anila.terminal") < body.index("data: [DONE]")


def test_with_terminal_does_not_duplicate_preemitted_terminal():
    """A generator that pre-emits its own anila.terminal (e.g. `error` on an
    agent failure) must NOT also get a `completed` from the wrapper — the
    override path that error/aborted/max_turns reasons rely on."""
    async def gen():
        yield router_server._make_chunk("hi", "anila-router")
        yield router_server._make_terminal("error")
        yield "data: [DONE]\n\n"

    async def collect():
        return [frame async for frame in router_server._with_terminal(gen())]

    body = "".join(asyncio.run(collect()))
    assert body.count("event: anila.terminal") == 1
    assert '"reason": "error"' in body
    assert '"reason": "completed"' not in body
