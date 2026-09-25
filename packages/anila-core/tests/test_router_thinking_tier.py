"""Router passthrough of a single-turn ``anila_thinking_tier`` override.

Contract (blueprint §2.3 Router):
  * caller body may send ``anila_thinking_tier`` = default|off|standard|deep
    (case-insensitive, trimmed). Other values are ignored.
  * Router forwards the canonical value on primary-model calls only
    (non-stream, stream, and same-turn auto-continue). Those helpers
    opt in via ``apply_thinking_tier=True``; default is off.
  * Compact summarizer, agent dispatch, and recompose must not see the key.
  * ``anila_sampling_defaults`` never lists it.
  * Upstream ``anila_meta.thinking_locked`` survives merge.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import respx
from fastapi.testclient import TestClient

from anila_core.api import router_server as rs
from anila_core.api.router_server import create_router_app
from anila_core.compact.openai_history import HISTORY_SUMMARY_PREFIX
from anila_core.config import settings
from anila_core.memory import close_all_connections
from anila_core.registry.remote_agent_manifest import (
    RemoteAgentManifest,
    RemoteAgentRegistry,
)


CSP_BASE = settings.csp_base_url
CSP_URL = f"{CSP_BASE}/v1/chat/completions"
CSP_AGENTS_URL = f"{CSP_BASE}/v1/agents"


class _Resp:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _StreamResp:
    def __init__(self, lines: list[str]):
        self.status_code = 200
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""


class _Client:
    def __init__(self, answers=None, streams=None):
        self.posts: list[dict] = []
        self.streams: list[dict] = []
        self._answers = list(answers or [])
        self._streams = list(streams or [])

    async def post(self, url, json=None, headers=None):
        self.posts.append(json)
        return _Resp(self._answers.pop(0))

    def stream(self, method, url, json=None, headers=None):
        self.streams.append(json)
        return _StreamResp(self._streams.pop(0))


def _reply(content: str, finish: str = "stop") -> dict:
    return {
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": finish}
        ]
    }


def _stream_lines(content: str, finish: str = "stop") -> list[str]:
    out = []
    if content:
        out.append("data: " + json.dumps({"choices": [{"delta": {"content": content}}]}))
    out.append("data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": finish}]}))
    out.append("data: [DONE]")
    return out


def _long_messages(turns: int = 8, size: int = 500) -> list[dict]:
    out = [{"role": "system", "content": "router"}]
    for i in range(turns):
        out.append({"role": "user", "content": f"u{i} " + ("問" * size)})
        out.append({"role": "assistant", "content": f"a{i} " + ("答" * size)})
    out.append({"role": "user", "content": "最新一問"})
    return out


def _with_tier(value: str | None):
    return rs.REQUEST_THINKING_TIER.set(value)


def _manifest(agent_id: str = "agent-a") -> RemoteAgentManifest:
    return RemoteAgentManifest(
        agent_id=agent_id,
        name=agent_id,
        description_for_router=f"Specialist {agent_id}",
        endpoint_url=f"http://{agent_id}",
    )


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


@pytest.fixture(autouse=True)
def _stub_router_refresh_hops(monkeypatch):
    """TestClient lifespan/request refresh would wait on a reachable CSP."""

    async def _noop_refresh() -> None:
        return None

    monkeypatch.setattr(rs, "refresh_router_model", _noop_refresh)
    monkeypatch.setattr(rs, "refresh_router_prompts", _noop_refresh)


@pytest_asyncio.fixture
async def db_path(tmp_path: Path):
    db = tmp_path / "router-thinking-tier.db"
    yield db
    await close_all_connections()


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("deep", "deep"),
        (" DEEP ", "deep"),
        ("Off", "off"),
        ("STANDARD", "standard"),
        ("default", "default"),
    ],
)
def test_thinking_tier_from_body_accepts_canonical_values(raw, expected):
    assert rs.thinking_tier_from_body({"anila_thinking_tier": raw}) == expected


@pytest.mark.parametrize("raw", ["turbo", 3, "", "  ", None, True, ["deep"]])
def test_thinking_tier_from_body_ignores_illegal_values(raw):
    assert rs.thinking_tier_from_body({"anila_thinking_tier": raw}) is None


def test_sampling_payload_omits_thinking_tier_unless_opted_in():
    token = _with_tier("deep")
    try:
        assert "anila_thinking_tier" not in rs._sampling_payload()
        params = rs._sampling_payload(apply_thinking_tier=True)
    finally:
        rs.REQUEST_THINKING_TIER.reset(token)
    assert params["anila_thinking_tier"] == "deep"
    assert "anila_thinking_tier" not in (params.get(rs.SAMPLING_DEFAULTS_MARKER) or [])


def test_sampling_payload_omits_thinking_tier_when_unset():
    assert "anila_thinking_tier" not in rs._sampling_payload()
    assert "anila_thinking_tier" not in rs._sampling_payload(apply_thinking_tier=True)


def test_non_stream_primary_payload_carries_thinking_tier(monkeypatch):
    client = _Client(answers=[_reply("答")])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)
    token = _with_tier("deep")
    try:
        result = asyncio.run(
            rs._call_llm_non_stream(
                "sk",
                [{"role": "user", "content": "q"}],
                apply_thinking_tier=True,
            )
        )
    finally:
        rs.REQUEST_THINKING_TIER.reset(token)
    assert result["content"] == "答"
    assert client.posts[0]["anila_thinking_tier"] == "deep"


def test_stream_primary_payload_carries_thinking_tier(monkeypatch):
    client = _Client(streams=[_stream_lines("答")])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)

    async def run():
        return [ev async for ev in rs._stream_llm_sse(
            "sk",
            [{"role": "user", "content": "q"}],
            apply_thinking_tier=True,
        )]

    token = _with_tier("deep")
    try:
        events = asyncio.run(run())
    finally:
        rs.REQUEST_THINKING_TIER.reset(token)
    assert any(ev.get("type") == "delta" and ev["content"] == "答" for ev in events)
    assert client.streams[0]["anila_thinking_tier"] == "deep"


def test_non_stream_omits_thinking_tier_unless_opted_in(monkeypatch):
    client = _Client(answers=[_reply("答"), _reply("答")])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)
    token = _with_tier("deep")
    try:
        asyncio.run(rs._call_llm_non_stream("sk", [{"role": "user", "content": "q"}]))
        asyncio.run(rs._call_llm_non_stream(
            "sk",
            [{"role": "user", "content": "q"}],
            apply_thinking_tier=True,
        ))
    finally:
        rs.REQUEST_THINKING_TIER.reset(token)
    assert "anila_thinking_tier" not in client.posts[0]
    assert client.posts[1]["anila_thinking_tier"] == "deep"


def test_non_stream_auto_continue_keeps_thinking_tier(monkeypatch):
    client = _Client(answers=[_reply("<html>", "length"), _reply("</html>", "stop")])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)
    token = _with_tier("deep")
    try:
        asyncio.run(rs._call_llm_non_stream(
            "sk",
            [{"role": "user", "content": "q"}],
            apply_thinking_tier=True,
        ))
    finally:
        rs.REQUEST_THINKING_TIER.reset(token)
    assert len(client.posts) == 2
    assert client.posts[0]["anila_thinking_tier"] == "deep"
    assert client.posts[1]["anila_thinking_tier"] == "deep"


def test_stream_auto_continue_keeps_thinking_tier(monkeypatch):
    client = _Client(
        streams=[
            _stream_lines("<!DOCTYPE html><html>", "length"),
            _stream_lines("</html>", "stop"),
        ]
    )
    monkeypatch.setattr(rs, "get_http_client", lambda: client)

    async def run():
        return [ev async for ev in rs._stream_llm_sse(
            "sk",
            [{"role": "user", "content": "q"}],
            apply_thinking_tier=True,
        )]

    token = _with_tier("deep")
    try:
        asyncio.run(run())
    finally:
        rs.REQUEST_THINKING_TIER.reset(token)
    assert len(client.streams) == 2
    assert client.streams[0]["anila_thinking_tier"] == "deep"
    assert client.streams[1]["anila_thinking_tier"] == "deep"


@pytest.mark.parametrize("bad", ["turbo", 3])
def test_illegal_thinking_tier_not_on_primary_payload(monkeypatch, bad):
    client = _Client(answers=[_reply("答")])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)
    parsed = rs.thinking_tier_from_body({"anila_thinking_tier": bad})
    token = _with_tier(parsed)
    try:
        asyncio.run(rs._call_llm_non_stream("sk", [{"role": "user", "content": "q"}]))
    finally:
        rs.REQUEST_THINKING_TIER.reset(token)
    assert parsed is None
    assert "anila_thinking_tier" not in client.posts[0]


def test_compact_summary_payload_omits_thinking_tier(monkeypatch):
    monkeypatch.setattr(rs, "current_router_context_window", lambda: 2_400)

    async def _summary_model():
        return "summary-llm"

    monkeypatch.setattr(rs, "resolve_summary_model_name", _summary_model)
    client = _Client(answers=[_reply("先前在做太陽系頁")])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)
    token = _with_tier("deep")
    try:
        compacted, step, _event = asyncio.run(
            rs._auto_compact_routing_messages(
                _long_messages(),
                caller_api_key="sk",
                forwarded_headers=None,
            )
        )
    finally:
        rs.REQUEST_THINKING_TIER.reset(token)
    assert step is not None
    assert HISTORY_SUMMARY_PREFIX in compacted[1]["content"]
    assert client.posts, "summarizer never called CSP"
    summary = client.posts[0]
    assert summary.get("model") == "summary-llm"
    assert "anila_thinking_tier" not in summary
    assert summary.get("reasoning_effort") == "none"


def test_compact_fails_when_summary_role_unset(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(rs, "current_router_context_window", lambda: 2_400)

    async def _unset():
        raise HTTPException(status_code=404, detail="摘要模型尚未在治理中心設定")

    monkeypatch.setattr(rs, "resolve_summary_model_name", _unset)
    client = _Client(answers=[_reply("不該被叫到")])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            rs._auto_compact_routing_messages(
                _long_messages(),
                caller_api_key="sk",
                forwarded_headers=None,
            )
        )
    assert exc.value.status_code == 404
    assert exc.value.detail == "摘要模型尚未在治理中心設定"
    assert client.posts == []


def test_recompose_payload_omits_thinking_tier(monkeypatch):
    client = _Client(answers=[_reply("潤飾後")])
    monkeypatch.setattr(rs, "get_http_client", lambda: client)
    token = _with_tier("deep")
    try:
        content, status = asyncio.run(rs._recompose_reply("原文", "sk"))
    finally:
        rs.REQUEST_THINKING_TIER.reset(token)
    assert status == "applied"
    assert content == "潤飾後"
    assert client.posts
    assert "anila_thinking_tier" not in client.posts[0]


def test_thinking_locked_survives_anila_meta_merge():
    merged = rs._merge_anila_meta(
        [],
        {"thinking_locked": True, "trace_id": "t-lock"},
    )
    assert merged["thinking_locked"] is True
    assert rs._normalize_anila_meta({"thinking_locked": True})["thinking_locked"] is True


@respx.mock
def test_dispatch_payload_omits_thinking_tier(db_path, monkeypatch):
    async def _no_per_request_model(request, caller_api_key, body):
        return None

    monkeypatch.setattr(rs, "_csp_resolve_router_model", _no_per_request_model)

    async def _skip_recompose(agent_reply, caller_api_key, *, forwarded_headers=None):
        return agent_reply, "skipped"

    monkeypatch.setattr(rs, "_recompose_reply", _skip_recompose)

    async def _fresh(self, api_key: str) -> None:
        self._agents = [_manifest()]

    def _list(self, api_key: str):
        return getattr(self, "_agents", [])

    def _get(self, api_key: str, agent_id: str):
        for m in getattr(self, "_agents", []):
            if m.agent_id == agent_id:
                return m
        return None

    monkeypatch.setattr(RemoteAgentRegistry, "ensure_fresh", _fresh)
    monkeypatch.setattr(RemoteAgentRegistry, "list_agents", _list)
    monkeypatch.setattr(RemoteAgentRegistry, "get", _get)

    seen = {"routing": [], "dispatch": []}

    def capture(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("model") == "agent-a":
            seen["dispatch"].append(body)
            return httpx.Response(200, json=_completion("agent answered", model="agent-a"))
        seen["routing"].append(body)
        return httpx.Response(200, json=_completion("DISPATCH:agent-a:查一下"))

    respx.post(CSP_URL).mock(side_effect=capture)
    respx.get(CSP_AGENTS_URL).mock(
        return_value=httpx.Response(200, json=_agents_payload(_manifest()))
    )

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test"},
        json={
            "messages": [{"role": "user", "content": "查一下"}],
            "stream": False,
            "anila_thinking_tier": "deep",
        },
    )
    assert resp.status_code == 200, resp.text
    assert seen["routing"], "routing LLM was never called"
    assert seen["routing"][0]["anila_thinking_tier"] == "deep"
    assert seen["dispatch"], "agent dispatch was never called"
    assert "anila_thinking_tier" not in seen["dispatch"][0]
