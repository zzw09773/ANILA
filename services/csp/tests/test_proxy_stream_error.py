"""Mid-stream / post-commit proxy failures must reach the client as anila.error.

When StreamingResponse has already committed HTTP 200, raising HTTPException
becomes ``Caught handled exception, but response already started`` and the
chat UI hangs. These tests lock the terminal SSE error path instead.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException

from app.services import proxy_service
from app.services.proxy import service as proxy_impl
from app.services.proxy.service import (
    format_anila_stream_error,
    stream_failure_user_message,
)


@pytest.fixture(autouse=True)
def _allow_mock_llm_endpoint(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")


class _FakeStreamResponse:
    def __init__(
        self,
        lines: list[str],
        status_code: int = 200,
        *,
        fail_after_lines: int | None = None,
        fail_exc: BaseException | None = None,
    ):
        self._lines = lines
        self.status_code = status_code
        self._fail_after_lines = fail_after_lines
        self._fail_exc = fail_exc or httpx.ReadError("connection reset")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_lines(self):
        for i, line in enumerate(self._lines):
            if self._fail_after_lines is not None and i >= self._fail_after_lines:
                raise self._fail_exc
            yield line
        # Fail after the last yielded line (true mid-stream / mid-body cut-off).
        if (
            self._fail_after_lines is not None
            and self._fail_after_lines >= len(self._lines)
        ):
            raise self._fail_exc


class _FakeAsyncClient:
    def __init__(self, response: _FakeStreamResponse, *args, **kwargs):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method: str, url: str, json: dict, headers: dict):
        return self._response


def _collect(monkeypatch, response: _FakeStreamResponse, **kwargs):
    monkeypatch.setattr(
        proxy_service.httpx,
        "AsyncClient",
        lambda *args, **kw: _FakeAsyncClient(response, *args, **kw),
    )

    async def fake_enqueue_usage(**_kwargs):
        return None

    monkeypatch.setattr(proxy_impl, "enqueue_usage", fake_enqueue_usage)
    monkeypatch.setattr(proxy_impl, "enqueue_usage_task_linked", fake_enqueue_usage)

    async def run():
        chunks: list[str] = []
        async for chunk in proxy_service.proxy_stream(
            target_url="http://mock-llm/v1/chat/completions",
            api_key_id=1,
            user_id=2,
            department_id=None,
            usage_model_id=3,
            request_body={
                "model": "google/gemma4",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            model_name=kwargs.get("model_name", "google/gemma4"),
            endpoint_display=kwargs.get("endpoint_display"),
        ):
            chunks.append(chunk)
        return chunks

    return asyncio.run(run())


def _error_payloads(chunks: list[str]) -> list[dict]:
    out: list[dict] = []
    for chunk in chunks:
        if "event: anila.error" not in chunk:
            continue
        for line in chunk.split("\n"):
            if line.startswith("data:"):
                out.append(json.loads(line[5:].strip()))
    return out


def test_mid_stream_failure_emits_terminal_error_and_keeps_prior_text(monkeypatch):
    """Upstream dies after some deltas → prior text still on the wire + anila.error."""
    lines = [
        'data: {"choices":[{"index":0,"delta":{"content":"Hel"},"finish_reason":null}]}',
        "",
        'data: {"choices":[{"index":0,"delta":{"content":"lo"},"finish_reason":null}]}',
        "",
        # fail before the next frame
    ]
    # After emitting both content frames (4 lines including blanks), blow up.
    chunks = _collect(
        monkeypatch,
        _FakeStreamResponse(lines, fail_after_lines=4),
    )
    joined = "".join(chunks)
    assert "Hel" in joined
    assert "lo" in joined
    errors = _error_payloads(chunks)
    assert len(errors) == 1
    assert "message" in errors[0]
    assert errors[0]["message"]
    # Must not raise — collecting the generator completed cleanly.
    assert "event: anila.error" in joined


def test_upstream_http_error_before_body_emits_anila_error(monkeypatch):
    """Status ≥400 before any body still cannot become an HTTP error response."""
    chunks = _collect(
        monkeypatch,
        _FakeStreamResponse([], status_code=404),
        model_name="demo-model",
    )
    errors = _error_payloads(chunks)
    assert len(errors) == 1
    assert "demo-model" in errors[0]["message"]
    assert "請" in errors[0]["message"]  # plain-language guidance


def test_stream_error_message_contains_no_endpoint_address(monkeypatch):
    """Ordinary-user path: error text must not carry host/port/URL."""
    leak = "https://aiagent2.ai.ncsist.org.tw:8443/v1/secret-path"
    chunks = _collect(
        monkeypatch,
        _FakeStreamResponse([], status_code=502),
        model_name="gpt-oss-20b",
        # Even if a privileged display form were somehow passed, the
        # user-facing anila.error formatter must not interpolate it.
        endpoint_display=leak,
    )
    errors = _error_payloads(chunks)
    assert len(errors) == 1
    msg = errors[0]["message"]
    assert leak not in msg
    assert "aiagent2" not in msg
    assert "8443" not in msg
    assert "ncsist" not in msg
    assert "http" not in msg.lower()
    assert "<owner-only>" not in msg
    assert "請" in msg


def test_stream_failure_user_message_never_echoes_exception_text():
    leak = "http://10.53.100.12/v1/chat/completions"
    msg = stream_failure_user_message(
        RuntimeError(f"upstream blew up at {leak}"),
        model_name="demo",
    )
    assert leak not in msg
    assert "10.53.100.12" not in msg
    assert "blew up" not in msg

    http_msg = stream_failure_user_message(
        HTTPException(status_code=404, detail=f"下游回應錯誤: {leak}"),
        model_name="demo",
    )
    assert leak not in http_msg
    assert "10.53.100.12" not in http_msg


def test_format_anila_stream_error_shape():
    block = format_anila_stream_error("模型暫時無法使用，請稍後再試。")
    assert block.startswith("event: anila.error\n")
    assert '"message"' in block
    assert block.endswith("\n\n")
