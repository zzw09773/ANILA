"""Tests for streaming usage accounting fallback in proxy_service."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.services import proxy_service
from app.services.proxy import service as proxy_impl


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
        ):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(run())
    joined = "".join(chunks)
    assert "event: anila.trace" in joined
    assert "event: anila.meta" in joined
    assert '"trace_id":"trace-1"' in joined


def test_proxy_stream_enforces_total_byte_ceiling(monkeypatch):
    lines = ['data: {"choices":[{"delta":{"content":"too large"}}]}', ""]
    monkeypatch.setattr(
        proxy_service.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(lines, *args, **kwargs),
    )
    monkeypatch.setattr(proxy_impl.settings, "PROXY_STREAM_MAX_BYTES", 8)

    async def run():
        async for _ in proxy_service.proxy_stream(
            target_url="http://mock-llm/v1/chat/completions",
            api_key_id=1,
            user_id=2,
            department_id=None,
            usage_model_id=3,
            request_body={"model": "m", "messages": []},
        ):
            pass

    with pytest.raises(HTTPException, match="資料量"):
        asyncio.run(run())


def test_cancel_after_usage_preserves_usage_in_failed_closure(monkeypatch):
    lines = [
        'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":5,"completion_tokens":3}}',
        "",
        'data: {"choices":[{"delta":{"content":"later"}}]}',
        "",
    ]
    monkeypatch.setattr(
        proxy_service.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(lines, *args, **kwargs),
    )
    monkeypatch.setattr(proxy_impl, "_lock_task_run_admission", lambda **_: "無機密")
    monkeypatch.setattr(proxy_impl, "_lock_registry_admission", lambda **_: None)
    monkeypatch.setattr(proxy_impl, "_commit_stream_admission", lambda _db: None)
    captured = []
    monkeypatch.setattr(
        proxy_impl,
        "persist_task_call_closure",
        lambda _db, closure: captured.append(closure),
    )

    class _DB:
        def rollback(self):
            pass

    async def run():
        stream = proxy_service.proxy_stream(
            target_url="http://mock-llm/v1/chat/completions",
            api_key_id=1,
            user_id=2,
            department_id=None,
            usage_model_id=3,
            request_body={"model": "m", "messages": []},
            task_id=10,
            task_trace_id="trace-10",
            task_run_id=11,
            governance_db=_DB(),
            admitted_classification_level="無機密",
        )
        await anext(stream)
        await stream.aclose()

    asyncio.run(run())
    assert captured
    assert captured[-1].status == "failed"
    assert captured[-1].usage.total_tokens == 8
