"""Tests for streaming usage accounting fallback in proxy_service."""

from __future__ import annotations

import asyncio

import pytest

from app.services import proxy_service
from app.services.proxy import service as proxy_impl

from app.services.proxy.service import ProxyTuning

#: 這些測試量的不是逾時／重試（那四顆在 ``test_settings_takes_effect_ops.py``），
#: 所以把它們釘在登錄表宣告的程式預設值上。``tuning`` 是必填的關鍵字參數：
#: production 的每一個呼叫點都要自己從 session 解一次，漏傳是 TypeError 而不是
#: 靜默凍結在預設值 —— 那個「必填」正是本包不想再出現假控制項的那道保險。
_PROXY_TUNING = ProxyTuning.from_registry_defaults()



@pytest.fixture(autouse=True)
def _allow_mock_llm_endpoint(monkeypatch):
    """proxy_stream now runs a call-time SSRF guard (#117 TOCTOU). The mock
    target ``http://mock-llm`` is single-label + http, so it only passes the
    guard with the dev allowances set (trusted host + http opt-in)."""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")


class _FakeStreamResponse:
    def __init__(self, lines: list[str], status_code: int = 200):
        self._lines = lines
        self.status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeAsyncClient:
    def __init__(self, lines: list[str], *args, **kwargs):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method: str, url: str, json: dict, headers: dict):
        return _FakeStreamResponse(self._lines)


def test_proxy_stream_estimates_usage_when_missing(monkeypatch):
    recorded: list[dict] = []
    lines = [
        'data: {"choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}',
        "",
        'data: {"choices":[{"index":0,"delta":{"content":" world"},"finish_reason":"stop"}]}',
        "",
        "data: [DONE]",
        "",
    ]

    monkeypatch.setattr(
        proxy_service.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(lines, *args, **kwargs),
    )

    async def fake_enqueue_usage(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(proxy_impl, "enqueue_usage", fake_enqueue_usage)

    async def run():
        chunks = []
        async for chunk in proxy_service.proxy_stream(
            target_url="http://mock-llm/v1/chat/completions",
            api_key_id=1,
            user_id=2,
            department_id=None,
            usage_model_id=3,
            request_body={
                "model": "google/gemma4",
                "messages": [{"role": "user", "content": "Say hello"}],
                "stream": True,
            },
            model_name="google/gemma4",
            tuning=_PROXY_TUNING,
        ):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(run())
    assert any("Hello" in chunk for chunk in chunks)
    assert recorded
    assert recorded[0]["prompt_tokens"] > 0
    assert recorded[0]["completion_tokens"] > 0
    assert recorded[0]["total_tokens"] == (
        recorded[0]["prompt_tokens"] + recorded[0]["completion_tokens"]
    )
    assert any("event: anila.meta" in chunk for chunk in chunks)


def test_proxy_stream_estimates_usage_from_message_content(monkeypatch):
    recorded: list[dict] = []
    lines = [
        'data: {"choices":[{"index":0,"message":{"role":"assistant","content":"Hello from agent"},"finish_reason":"stop"}]}',
        "",
        "data: [DONE]",
        "",
    ]

    monkeypatch.setattr(
        proxy_service.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(lines, *args, **kwargs),
    )

    async def fake_enqueue_usage(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(proxy_impl, "enqueue_usage", fake_enqueue_usage)

    async def run():
        async for _chunk in proxy_service.proxy_stream(
            target_url="http://mock-llm/v1/chat/completions",
            api_key_id=1,
            user_id=2,
            department_id=None,
            usage_model_id=3,
            request_body={
                "model": "google/gemma4",
                "messages": [{"role": "user", "content": "Say hello"}],
                "stream": True,
            },
            model_name="google/gemma4",
            tuning=_PROXY_TUNING,
        ):
            pass

    asyncio.run(run())
    assert recorded
    assert recorded[0]["completion_tokens"] > 0


def test_proxy_stream_prefers_upstream_usage(monkeypatch):
    recorded: list[dict] = []
    lines = [
        'data: {"choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}',
        "",
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":11,"completion_tokens":7}}',
        "",
        "data: [DONE]",
        "",
    ]

    monkeypatch.setattr(
        proxy_service.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(lines, *args, **kwargs),
    )

    async def fake_enqueue_usage(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(proxy_impl, "enqueue_usage", fake_enqueue_usage)

    async def run():
        async for _chunk in proxy_service.proxy_stream(
            target_url="http://mock-llm/v1/chat/completions",
            api_key_id=1,
            user_id=2,
            department_id=None,
            usage_model_id=3,
            request_body={
                "model": "google/gemma4",
                "messages": [{"role": "user", "content": "Say hello"}],
                "stream": True,
            },
            model_name="google/gemma4",
            tuning=_PROXY_TUNING,
        ):
            pass

    asyncio.run(run())
    assert recorded == [{
        "api_key_id": 1,
        "user_id": 2,
        "department_id": None,
        "model_id": 3,
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
        "request_duration_ms": recorded[0]["request_duration_ms"],
        "conversation_id": None,
        "trace_id": None,
        "caller_agent_id": None,
        "caller_client_id": None,
    }]


def test_proxy_stream_preserves_custom_anila_events(monkeypatch):
    lines = [
        "event: anila.trace",
        'data: {"kind":"call","label":"Invoke agent","detail":"demo","status":"ok"}',
        "",
        'data: {"choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}',
        "",
        "event: anila.meta",
        'data: {"trace_id":"trace-1","trace":[],"citations":[],"confidence":null,"handoff_chain":[],"follow_ups":[],"latency_ms":3,"classified":false}',
        "",
        "data: [DONE]",
        "",
    ]

    monkeypatch.setattr(
        proxy_service.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(lines, *args, **kwargs),
    )

    async def fake_enqueue_usage(**kwargs):
        return None

    monkeypatch.setattr(proxy_impl, "enqueue_usage", fake_enqueue_usage)

    async def run():
        chunks = []
        async for chunk in proxy_service.proxy_stream(
            target_url="http://mock-llm/v1/chat/completions",
            api_key_id=1,
            user_id=2,
            department_id=None,
            usage_model_id=3,
            request_body={
                "model": "google/gemma4",
                "messages": [{"role": "user", "content": "Say hello"}],
                "stream": True,
            },
            model_name="google/gemma4",
            tuning=_PROXY_TUNING,
        ):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(run())
    joined = "".join(chunks)
    assert "event: anila.trace" in joined
    assert "event: anila.meta" in joined
    assert '"trace_id":"trace-1"' in joined
