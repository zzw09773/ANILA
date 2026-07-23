"""Tests for streaming usage accounting fallback in proxy_service."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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

    async def aiter_bytes(self, chunk_size: int | None = None):
        del chunk_size
        for line in self._lines:
            yield (line + "\n").encode()


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


def test_proxy_stream_enforces_byte_ceiling_before_no_newline_buffer_grows(
    monkeypatch,
):
    consumed = 0

    class _NoNewlineResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def aiter_bytes(self, chunk_size: int | None = None):
            nonlocal consumed
            assert chunk_size == 64 * 1024
            for _ in range(1000):
                consumed += 1
                yield b"abcd"

    class _NoNewlineClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        def stream(self, *_args, **_kwargs):
            return _NoNewlineResponse()

    monkeypatch.setattr(
        proxy_service.httpx, "AsyncClient", lambda *_args, **_kwargs: _NoNewlineClient()
    )
    monkeypatch.setattr(proxy_impl.settings, "PROXY_STREAM_MAX_BYTES", 10)

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
    assert consumed == 3


def test_proxy_stream_preserves_event_ceiling(monkeypatch):
    lines = [
        'data: {"choices":[{"delta":{"content":"one"}}]}',
        "",
        'data: {"choices":[{"delta":{"content":"two"}}]}',
        "",
    ]
    monkeypatch.setattr(
        proxy_service.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(lines, *args, **kwargs),
    )
    monkeypatch.setattr(proxy_impl.settings, "PROXY_STREAM_MAX_EVENTS", 1)

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

    with pytest.raises(HTTPException, match="事件數"):
        asyncio.run(run())


def test_task_stream_deadline_includes_pre_stream_elapsed(monkeypatch):
    client_opened = False

    def unexpected_client(*_args, **_kwargs):
        nonlocal client_opened
        client_opened = True
        raise AssertionError("expired request must not open an upstream client")

    monkeypatch.setattr(proxy_service.httpx, "AsyncClient", unexpected_client)
    monkeypatch.setattr(proxy_impl.settings, "PROXY_STREAM_MAX_SECONDS", 1.0)
    monkeypatch.setattr(
        proxy_impl, "_lock_task_run_admission", lambda **_: "無機密"
    )
    monkeypatch.setattr(proxy_impl, "_lock_registry_admission", lambda **_: None)
    monkeypatch.setattr(proxy_impl, "_commit_stream_admission", lambda _db: None)
    closures = []
    monkeypatch.setattr(
        proxy_impl,
        "persist_task_call_closure",
        lambda _db, closure: closures.append(closure),
    )

    class _DB:
        def rollback(self):
            pass

    async def run():
        async for _ in proxy_service.proxy_stream(
            target_url="http://mock-llm/v1/chat/completions",
            api_key_id=1,
            user_id=2,
            department_id=None,
            usage_model_id=3,
            request_body={"model": "m", "messages": []},
            task_id=10,
            task_trace_id="trace-10",
            task_run_id=11,
            task_run_started_at=(
                datetime.now(timezone.utc) - timedelta(seconds=2)
            ),
            governance_db=_DB(),
            admitted_classification_level="無機密",
        ):
            pass

    with pytest.raises(HTTPException, match="總時限"):
        asyncio.run(run())

    assert not client_opened
    assert closures
    assert closures[-1].status == "failed"


def test_stream_deadline_interrupts_silent_upstream(monkeypatch):
    class _SilentResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def aiter_lines(self):
            await asyncio.sleep(10)
            yield "data: never reached"

        async def aiter_bytes(self, chunk_size: int | None = None):
            del chunk_size
            await asyncio.sleep(10)
            yield b"data: never reached\n"

    class _SilentClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, *_args, **_kwargs):
            return _SilentResponse()

    monkeypatch.setattr(
        proxy_service.httpx,
        "AsyncClient",
        lambda *_args, **_kwargs: _SilentClient(),
    )
    monkeypatch.setattr(proxy_impl.settings, "PROXY_STREAM_MAX_SECONDS", 0.05)

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

    with pytest.raises(HTTPException, match="總時限"):
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


# --------------------------------------------------------------------------
# Internal-router orchestration hop: the anila-router sentinel forward is an
# orchestration hop, not a terminal model call.  ``suppress_usage_accounting``
# (threaded from ``_is_internal_router_model(model)`` at the proxy call sites)
# must stop the outer forward from enqueuing / capturing any token_usage row,
# so the nested real-model inference is counted exactly once and never
# double-counted.  Governance/PolicyDecision logging is unaffected — only the
# token_usage accounting is suppressed.
# --------------------------------------------------------------------------


def _run_stream(recorded, monkeypatch, lines, **kwargs):
    monkeypatch.setattr(
        proxy_service.httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeAsyncClient(lines, *a, **kw),
    )

    async def fake_enqueue_usage(**kw):
        recorded.append(kw)

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
            **kwargs,
        ):
            pass

    asyncio.run(run())


_USAGE_LINES = [
    'data: {"choices":[{"index":0,"delta":{"content":"Hi"},"finish_reason":null}]}',
    "",
    'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":11,"completion_tokens":7}}',
    "",
    "data: [DONE]",
    "",
]


def test_stream_suppresses_usage_for_internal_router_hop(monkeypatch):
    recorded: list[dict] = []
    _run_stream(
        recorded, monkeypatch, _USAGE_LINES, suppress_usage_accounting=True
    )
    # Upstream reported usage, but the orchestration hop must not enqueue it.
    assert recorded == []


def test_stream_writes_single_row_when_not_suppressed(monkeypatch):
    recorded: list[dict] = []
    _run_stream(
        recorded, monkeypatch, _USAGE_LINES, suppress_usage_accounting=False
    )
    assert len(recorded) == 1
    assert recorded[0]["model_id"] == 3
    assert recorded[0]["total_tokens"] == 18


def test_stream_task_linked_router_hop_writes_no_usage_row(monkeypatch):
    """Task-linked sentinel forward: the durable closure still finalizes the
    TaskRun, but carries no usage so no TokenUsage row is written."""
    monkeypatch.setattr(proxy_impl, "_lock_task_run_admission", lambda **_: "無機密")
    monkeypatch.setattr(proxy_impl, "_lock_registry_admission", lambda **_: None)
    monkeypatch.setattr(proxy_impl, "_commit_stream_admission", lambda _db: None)
    monkeypatch.setattr(
        proxy_service.httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeAsyncClient(_USAGE_LINES, *a, **kw),
    )
    captured: list = []
    monkeypatch.setattr(
        proxy_impl,
        "persist_task_call_closure",
        lambda _db, closure: captured.append(closure),
    )

    class _DB:
        def rollback(self):
            pass

    async def run():
        async for _ in proxy_service.proxy_stream(
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
            suppress_usage_accounting=True,
        ):
            pass

    asyncio.run(run())
    assert captured
    assert captured[-1].status == "completed"
    # No usage attached -> closure writes no TokenUsage row.
    assert captured[-1].usage is None


class _FakeJSONResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeJSONClient:
    def __init__(self, payload: dict, *args, **kwargs):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json, headers):
        del url, json, headers
        return _FakeJSONResponse(self._payload)


def _run_request(recorded, monkeypatch, *, suppress: bool):
    payload = {
        "choices": [{"message": {"role": "assistant", "content": "hi"}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }
    monkeypatch.setattr(
        proxy_service.httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeJSONClient(payload, *a, **kw),
    )
    monkeypatch.setattr(proxy_impl, "resolve_model_gateway_key", lambda _m: None)

    async def fake_enqueue_usage(**kw):
        recorded.append(kw)

    monkeypatch.setattr(proxy_impl, "enqueue_usage", fake_enqueue_usage)

    model = SimpleNamespace(
        endpoint_url="http://mock-llm",
        model_type="chat",
        api_version="v1",
        name="google/gemma4",
        id=3,
    )

    async def run():
        return await proxy_service.proxy_request(
            model=model,
            api_key_id=1,
            user_id=2,
            department_id=None,
            request_body={"messages": [{"role": "user", "content": "hi"}]},
            endpoint_path="/v1/chat/completions",
            suppress_usage_accounting=suppress,
        )

    return asyncio.run(run())


def test_nonstream_suppresses_usage_for_internal_router_hop(monkeypatch):
    recorded: list[dict] = []
    result = _run_request(recorded, monkeypatch, suppress=True)
    assert result["usage"]["total_tokens"] == 18  # response passthrough intact
    assert recorded == []


def test_nonstream_writes_single_row_when_not_suppressed(monkeypatch):
    recorded: list[dict] = []
    _run_request(recorded, monkeypatch, suppress=False)
    assert len(recorded) == 1
    assert recorded[0]["model_id"] == 3
    assert recorded[0]["total_tokens"] == 18


def test_stream_fallback_meta_masks_identity_and_never_leaks_url(monkeypatch):
    """串流 fallback meta 與非串流同規則:ANILA 編排(service caller)遮模型名;
    內部端點 URL 任何情況都不得進使用者可見 meta。"""
    lines = [
        'data: {"choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":"stop"}]}',
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
        pass

    monkeypatch.setattr(proxy_impl, "enqueue_usage", fake_enqueue_usage)

    def run(caller_client_id):
        async def _run():
            chunks = []
            async for chunk in proxy_service.proxy_stream(
                target_url="http://mock-llm:9000/v1/chat/completions",
                api_key_id=1,
                user_id=2,
                department_id=None,
                usage_model_id=3,
                request_body={"model": "gemma26", "stream": True},
                model_name="gemma26",
                caller_client_id=caller_client_id,
            ):
                chunks.append(chunk)
            return "".join(chunks)

        return asyncio.run(_run())

    orchestrated = run(caller_client_id=42)
    assert "mock-llm:9000" not in orchestrated
    assert "gemma26" not in orchestrated.split("event: anila.meta", 1)[1]
    assert "ANILA" in orchestrated

    manual = run(caller_client_id=None)
    assert "mock-llm:9000" not in manual
    assert "呼叫 gemma26" in manual
