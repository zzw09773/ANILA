"""C-period: compact boundary event + manual ``POST /v1/conversations/compact``."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import respx
from fastapi.testclient import TestClient

from anila_core.api import router_prompts
from anila_core.api import router_server as rs
from anila_core.api.router_server import create_router_app
from anila_core.compact.openai_history import (
    HISTORY_SUMMARY_PREFIX,
    _flatten_turns,
    _split_turns,
    auto_compact_openai_messages,
)
from anila_core.config import settings
from anila_core.memory import close_all_connections
from anila_core.registry.remote_agent_manifest import RemoteAgentRegistry


CSP_BASE = settings.csp_base_url
CSP_RESOLVE_URL = f"{CSP_BASE}/api/router-models/resolve"
CSP_CHAT_URL = f"{CSP_BASE}/v1/chat/completions"


class _Resp:
    def __init__(self, payload: dict, status: int = 200, text: str | None = None):
        self._payload = payload
        self.status_code = status
        self.text = text if text is not None else json.dumps(payload)
        self._request = httpx.Request("POST", "http://csp/v1/chat/completions")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "overflow",
                request=self._request,
                response=httpx.Response(self.status_code, text=self.text, request=self._request),
            )

    def json(self):
        return self._payload


class _Client:
    def __init__(self, answers=None):
        self.posts: list[dict] = []
        self._answers = list(answers or [])

    async def post(self, url, json=None, headers=None):
        self.posts.append(json)
        item = self._answers.pop(0)
        return item if isinstance(item, _Resp) else _Resp(item)


def _reply(content: str) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]
    }


def _long_messages(turns: int = 8, size: int = 500) -> list[dict]:
    out = [{"role": "system", "content": "router"}]
    for i in range(turns):
        out.append({"role": "user", "content": f"u{i} " + ("問" * size)})
        out.append({"role": "assistant", "content": f"a{i} " + ("答" * size)})
    out.append({"role": "user", "content": "最新一問"})
    return out


def _expected_kept_from_index(inbound: list[dict], keep_recent_turns: int = 4) -> int:
    _, turns = _split_turns([dict(m) for m in inbound])
    keep_n = max(1, keep_recent_turns)
    if len(turns) > keep_n:
        recent = _flatten_turns(turns[-keep_n:])
    elif len(turns) > 1:
        recent = _flatten_turns(turns[-1:])
    else:
        recent = _flatten_turns(turns)
    return len(inbound) - len(recent)


def _strip_image_messages() -> list[dict]:
    """Long enough to compact, but peeling old data URLs is enough."""
    big = "G" * 8000
    out: list[dict] = [{"role": "system", "content": "router"}]
    for i in range(7):
        if i < 3:
            content: object = [
                {"type": "text", "text": f"u{i} 圖"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{big}"}},
            ]
        else:
            content = f"u{i} 文字"
        out.append({"role": "user", "content": content})
        out.append({"role": "assistant", "content": f"a{i}"})
    out.append({"role": "user", "content": "最新一問"})
    return out


def _parse_named_events(body: str, name: str) -> list[dict]:
    events = []
    for block in body.split("\n\n"):
        lines = block.split("\n")
        if f"event: {name}" not in lines:
            continue
        for line in lines:
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def _assert_compact_payload(
    payload: dict,
    messages: list[dict],
    *,
    keep_recent_turns: int = 4,
    expect_keep_n: bool = False,
) -> None:
    assert payload["method"] == "summary"
    assert payload["summary"]
    assert HISTORY_SUMMARY_PREFIX not in payload["summary"]
    idx = payload["kept_from_index"]
    assert 0 <= idx < len(messages)
    if expect_keep_n:
        assert idx == _expected_kept_from_index(messages, keep_recent_turns)
    else:
        # Auto-compact may sliding-window the tail after a long summary;
        # kept_from_index must still land on a real inbound turn.
        assert messages[idx]["role"] in {"user", "assistant"}
    assert payload["tokens_before"] > payload["tokens_after"]


@pytest_asyncio.fixture
async def db_path(tmp_path: Path):
    db = tmp_path / "router-compact-events.db"
    yield db
    await close_all_connections()


async def _no_per_request_model(request, caller_api_key, body):
    return None


def _install_empty_registry(monkeypatch):
    async def fake_ensure_fresh(self, api_key: str) -> None:
        self._agents = []

    def fake_list_agents(self, api_key: str):
        return getattr(self, "_agents", [])

    def fake_get(self, api_key: str, agent_id: str):
        return None

    monkeypatch.setattr(RemoteAgentRegistry, "ensure_fresh", fake_ensure_fresh)
    monkeypatch.setattr(RemoteAgentRegistry, "list_agents", fake_list_agents)
    monkeypatch.setattr(RemoteAgentRegistry, "get", fake_get)


def _install_direct_llm(monkeypatch):
    async def fake_llm(api_key, messages, *, forwarded_headers=None):
        return {
            "content": "好",
            "reasoning": None,
            "anila_meta": None,
            "raw": None,
            "error": None,
        }

    async def fake_stream(*_args, **_kwargs):
        yield {"type": "delta", "content": "好"}
        yield {"type": "done", "finish_reason": "stop"}

    monkeypatch.setattr(rs, "_call_llm_non_stream", fake_llm)
    monkeypatch.setattr(rs, "_stream_llm_sse", fake_stream)


def _install_summary(monkeypatch, text: str = "濃縮過的太陽系討論"):
    async def fake_summary(key, old, headers):
        assert any("u0" in str(m.get("content")) for m in old)
        return text

    monkeypatch.setattr(rs, "_summarize_for_compact", fake_summary)


def _chat_client(db_path, monkeypatch, *, window: int = 2_400):
    monkeypatch.setattr(rs, "_csp_resolve_router_model", _no_per_request_model)
    monkeypatch.setattr(rs, "current_router_context_window", lambda: window)
    _install_empty_registry(monkeypatch)
    _install_direct_llm(monkeypatch)
    return TestClient(create_router_app(session_db_path=str(db_path)))


def test_streaming_emits_anila_compact(db_path, monkeypatch):
    _install_summary(monkeypatch)
    client = _chat_client(db_path, monkeypatch)
    messages = _long_messages()
    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test"},
        json={"messages": messages, "stream": True},
    ) as response:
        body = "".join(response.iter_text())
    events = _parse_named_events(body, "anila.compact")
    assert events, body
    _assert_compact_payload(events[0], messages)


def test_non_stream_anila_meta_compact(db_path, monkeypatch):
    _install_summary(monkeypatch)
    client = _chat_client(db_path, monkeypatch)
    messages = _long_messages()
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test"},
        json={"messages": messages, "stream": False},
    )
    assert resp.status_code == 200, resp.text
    compact = resp.json()["anila_meta"]["compact"]
    _assert_compact_payload(compact, messages)


def test_prior_summary_prepended_to_summarizer_transcript(monkeypatch):
    monkeypatch.setattr(rs, "current_router_context_window", lambda: 2_400)
    http = _Client(answers=[_reply("新的摘要")])
    monkeypatch.setattr(rs, "get_http_client", lambda: http)
    messages = [{"role": "system", "content": "[歷史摘要]\n先前談過太陽系"}]
    for i in range(8):
        messages.append({"role": "user", "content": f"u{i} " + ("問" * 500)})
        messages.append({"role": "assistant", "content": f"a{i} " + ("答" * 500)})
    messages.append({"role": "user", "content": "最新一問"})

    compacted, step, event = asyncio.run(
        rs._auto_compact_routing_messages(
            messages,
            caller_api_key="sk",
            forwarded_headers=None,
        )
    )
    assert step is not None
    assert event is not None
    assert http.posts, "summarizer never called CSP"
    transcript = http.posts[0]["messages"][1]["content"]
    assert transcript.startswith("先前摘要：先前談過太陽系")
    assert HISTORY_SUMMARY_PREFIX not in event["summary"]


def test_strip_images_does_not_emit_anila_compact(db_path, monkeypatch):
    async def summarizer_must_not_run(*_a, **_k):
        raise AssertionError("summarizer should not run when strip_images is enough")

    monkeypatch.setattr(rs, "_summarize_for_compact", summarizer_must_not_run)
    client = _chat_client(db_path, monkeypatch, window=4_000)
    messages = _strip_image_messages()
    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test"},
        json={"messages": messages, "stream": True},
    ) as response:
        body = "".join(response.iter_text())
    assert _parse_named_events(body, "anila.compact") == []
    assert "event: anila.compact" not in body

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test"},
        json={"messages": messages, "stream": False},
    )
    assert resp.status_code == 200, resp.text
    assert "compact" not in (resp.json().get("anila_meta") or {})


def test_normalize_anila_meta_passes_compact_through():
    payload = {
        "summary": "x",
        "kept_from_index": 3,
        "method": "summary",
        "tokens_before": 10,
        "tokens_after": 4,
    }
    out = rs._normalize_anila_meta({"compact": payload, "trace_id": "t-c"})
    assert out["compact"] == payload


@respx.mock
def test_manual_compact_summarizes_long_conversation(db_path):
    respx.post(CSP_RESOLVE_URL).mock(
        return_value=httpx.Response(200, json={"name": "glm-test"})
    )
    respx.post(CSP_CHAT_URL).mock(
        return_value=httpx.Response(200, json=_reply("手動整理後的摘要"))
    )
    messages = _long_messages()
    client = TestClient(create_router_app(session_db_path=str(db_path)))
    resp = client.post(
        "/v1/conversations/compact",
        headers={"Authorization": "Bearer sk-test"},
        json={"messages": messages, "router_model": "glm-test"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    _assert_compact_payload(body, messages, keep_recent_turns=2, expect_keep_n=True)
    assert body["method"] == "summary"


@respx.mock
def test_manual_compact_one_turn_is_none(db_path):
    respx.post(CSP_RESOLVE_URL).mock(
        return_value=httpx.Response(200, json={"name": "glm-test"})
    )
    messages = [
        {"role": "user", "content": "只有一問"},
        {"role": "assistant", "content": "只有一答"},
    ]
    client = TestClient(create_router_app(session_db_path=str(db_path)))
    resp = client.post(
        "/v1/conversations/compact",
        headers={"Authorization": "Bearer sk-test"},
        json={"messages": messages},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["method"] == "none"
    assert body["summary"] is None
    assert body["kept_from_index"] == len(messages)


def test_manual_compact_and_chat_require_auth(db_path):
    client = TestClient(create_router_app(session_db_path=str(db_path)))
    messages = [{"role": "user", "content": "hi"}]
    compact = client.post("/v1/conversations/compact", json={"messages": messages})
    chat = client.post("/v1/chat/completions", json={"messages": messages})
    assert compact.status_code == 401
    assert chat.status_code == 401
    assert compact.json()["detail"] == chat.json()["detail"]


@pytest.mark.asyncio
async def test_force_summary_is_not_cut_again_by_sliding_window():
    messages = _long_messages(8, size=600)

    async def summarize(old):
        return "短摘要"

    result = await auto_compact_openai_messages(
        messages,
        context_window=2_400,
        max_output_tokens=256,
        summarizer=summarize,
        keep_recent_turns=2,
        force=True,
    )
    assert result.method == "summary"
    assert result.summary == "短摘要"
    assert HISTORY_SUMMARY_PREFIX in result.messages[1]["content"]
    assert not any(
        "[Earlier conversation history was truncated" in str(m.get("content"))
        for m in result.messages
    )
    assert result.recent_count > 0


def test_prior_summary_extracted_from_plain_assistant_fold():
    system = router_prompts.DEFAULT_PLAIN_ASSISTANT + "\n\n[歷史摘要]\n先前談過太陽系"
    prior = rs._extract_prior_history_summary([{"role": "system", "content": system}])
    assert prior == "先前談過太陽系"
    assert "平台身分" not in prior
    assert "【" not in prior
    assert "你是 ANILA" not in prior


def test_oversized_summary_event_kept_from_index_points_at_outbound(monkeypatch):
    monkeypatch.setattr(rs, "current_router_context_window", lambda: 2_400)

    async def fake_summary(key, old, headers):
        return "摘" * 5_000

    monkeypatch.setattr(rs, "_summarize_for_compact", fake_summary)
    messages = _long_messages()
    compacted, step, event = asyncio.run(
        rs._auto_compact_routing_messages(
            messages,
            caller_api_key="sk",
            forwarded_headers=None,
            inbound_message_count=len(messages),
        )
    )
    assert step is not None
    assert event is not None
    assert compacted[1]["content"].startswith(HISTORY_SUMMARY_PREFIX)
    assert event["method"] == "summary"
    expected_recent = sum(
        1
        for m in compacted
        if m.get("role") != "system"
        and not str(m.get("content") or "").lstrip().startswith(HISTORY_SUMMARY_PREFIX)
    )
    assert event["kept_from_index"] == len(messages) - expected_recent
    kept = messages[event["kept_from_index"]]
    assert any(
        m.get("role") == kept.get("role") and m.get("content") == kept.get("content")
        for m in compacted
    )
