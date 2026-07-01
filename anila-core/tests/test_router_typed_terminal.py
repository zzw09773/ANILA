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

    # Mock the streaming LLM too so the direct-answer path is a clean success
    # (no error trace) — otherwise _stream_llm_sse hits a real backend, errors,
    # and the turn is correctly labelled `error`, not `completed`.
    async def fake_stream_llm(*args, **kwargs):
        yield {"type": "content", "content": "直接回答內容"}

    monkeypatch.setattr(router_server, "_stream_llm_sse", fake_stream_llm)

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


def test_with_terminal_handles_resume_style_single_newline_done():
    """The resume passthrough re-emits agent SSE via httpx aiter_lines(), which
    strips SSE terminators — so `[DONE]` arrives as ``data: [DONE]\\n`` (one
    newline) + a separate ``\\n``. The wrapper must STILL inject exactly one
    terminal (frozen contract §6: one per turn), not miss it on an exact match."""
    async def gen():
        yield router_server._make_chunk("hi", "anila-router")
        yield "data: [DONE]\n"   # resume-style single-newline DONE frame
        yield "\n"               # trailing blank line as its own frame

    async def collect():
        return [frame async for frame in router_server._with_terminal(gen())]

    body = "".join(asyncio.run(collect()))
    assert body.count("event: anila.terminal") == 1
    assert '"reason": "completed"' in body
    assert body.index("event: anila.terminal") < body.index("data: [DONE]")


def test_with_terminal_marks_error_on_real_error_trace():
    """A real turn error (non-registry anila.trace status=error — LLM outage /
    agent failure / route-miss) reaching [DONE] with no pre-emitted terminal must
    be labelled `error`, not the `completed` default — else the UI shows a genuine
    failure as a normal answer (review root-cause 1)."""
    async def gen():
        yield router_server._make_event(
            "anila.trace",
            router_server._make_trace_step("direct", "LLM 無法回應", "boom", status="error"),
        )
        yield router_server._make_chunk("（LLM 暫時無法回應）", "anila-router")
        yield "data: [DONE]\n\n"

    async def collect():
        return [frame async for frame in router_server._with_terminal(gen())]

    body = "".join(asyncio.run(collect()))
    assert body.count("event: anila.terminal") == 1
    assert '"reason": "error"' in body
    assert '"reason": "completed"' not in body


def test_with_terminal_ignores_nonfatal_registry_error_trace():
    """The non-fatal registry-refresh warning (kind=registry, status=error) must
    NOT flip the terminal to `error` — the turn still completed normally. Guards
    the false-positive that made the earlier trace-scan approach wrong."""
    async def gen():
        yield router_server._make_event(
            "anila.trace",
            router_server._make_trace_step(
                "registry", "同步 agent 清單", "refresh failed", status="error"
            ),
        )
        yield router_server._make_chunk("正常答案", "anila-router")
        yield "data: [DONE]\n\n"

    async def collect():
        return [frame async for frame in router_server._with_terminal(gen())]

    body = "".join(asyncio.run(collect()))
    assert '"reason": "completed"' in body
    assert '"reason": "error"' not in body


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
