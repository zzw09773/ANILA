"""Task 5 — 非串流 proxy 接入 backend adapter.

Locks the contract: ``_proxy_request_impl`` (the non-stream forwarding
path) resolves a ``BackendAdapter`` from ``model.protocol`` and applies it
around the outbound call:

- ``adapter.request_timeout`` derives the httpx timeout from the existing
  per-model-type default.
- ``adapter.to_backend_request`` transforms the outbound body before it is
  POSTed upstream.
- ``adapter.from_backend_response`` transforms the parsed 2xx result
  (including the SSE-fallback-aggregated shape) before it is returned.
- ``adapter.from_backend_error`` transforms the non-2xx (4xx) upstream body
  before its ``message`` is surfaced as the HTTPException detail.

``openai_compatible`` (the default/only protocol wired up so far) resolves
to ``PassthroughAdapter``, whose hooks are no-ops for the request/response
shape, so the existing behavior for that path is unchanged — this suite
exercises both a spy adapter (to prove the wiring calls each hook exactly
once with the right endpoint_kind) and the real passthrough registry (to
lock the no-regression path).
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from app.services.proxy import service as proxy_impl

from tests.conftest import make_model


@pytest.fixture(autouse=True)
def _allow_mock_llm_endpoint(monkeypatch):
    """Same dev SSRF allowance as test_proxy_stream_usage.py — the mock
    target ``http://mock-llm`` is single-label + http, so it only passes
    the call-time guard with these dev allowances set."""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")


@pytest.fixture(autouse=True)
def _stub_usage_queue(monkeypatch):
    """Usage accounting is orthogonal to adapter wiring; stub both enqueue
    paths like test_proxy_stream_usage.py does so no real queue/DB write is
    attempted."""

    async def _fake(**kwargs):
        return None

    monkeypatch.setattr(proxy_impl, "enqueue_usage", _fake)
    monkeypatch.setattr(proxy_impl, "enqueue_usage_task_linked", _fake)


class _PostResponse:
    def __init__(self, payload, status_code: int = 200):
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}
        if isinstance(payload, str):
            self.text = payload
            self._payload = None
        else:
            self._payload = payload
            self.text = json.dumps(payload)

    def json(self):
        if self._payload is None:
            return json.loads(self.text)
        return self._payload


class _PostClient:
    """Fake httpx.AsyncClient — same shape as test_proxy_task_wiring.py's
    ``_PostClient``: records nothing, just returns a canned response."""

    def __init__(self, payload, status_code: int, *args, **kwargs):
        self._payload = payload
        self._status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        return _PostResponse(self._payload, self._status_code)


def _patch_client(monkeypatch, payload, status_code: int = 200):
    monkeypatch.setattr(
        proxy_impl.httpx,
        "AsyncClient",
        lambda *a, **k: _PostClient(payload, status_code, *a, **k),
    )


class _SpyAdapter:
    """Records every hook invocation (name + endpoint_kind seen) and marks
    its output so assertions can prove the wiring actually threads the
    adapter's return value back into the proxy result, rather than just
    calling it and discarding the output."""

    name = "spy_protocol"

    def __init__(self):
        self.calls: list[str] = []
        self.kinds: dict[str, str] = {}

    def request_timeout(self, endpoint_kind, default):
        self.calls.append("timeout")
        self.kinds["timeout"] = endpoint_kind
        return default

    def backend_path(self, endpoint_kind, model_name, api_version):
        return f"/{api_version}/chat/completions"

    def to_backend_request(self, endpoint_kind, body):
        self.calls.append("req")
        self.kinds["req"] = endpoint_kind
        return {**body, "_adapted_request": True}

    def from_backend_response(self, endpoint_kind, resp):
        self.calls.append("resp")
        self.kinds["resp"] = endpoint_kind
        return {**resp, "_adapted_response": True}

    def from_backend_stream_chunk(self, endpoint_kind, raw_line):
        return raw_line

    def from_backend_error(self, endpoint_kind, status, raw_body):
        self.calls.append("err")
        self.kinds["err"] = endpoint_kind
        return {"error": {"message": f"adapted:{raw_body}"}}


def _install_spy(monkeypatch) -> _SpyAdapter:
    spy = _SpyAdapter()
    monkeypatch.setattr(proxy_impl, "get_adapter", lambda protocol: spy)
    return spy


async def _call(model, request_body=None, endpoint_path="/v1/chat/completions"):
    return await proxy_impl._proxy_request_impl(
        model=model,
        api_key_id=1,
        user_id=1,
        department_id=None,
        request_body=request_body or {"model": model.name, "messages": []},
        endpoint_path=endpoint_path,
        user_identity="123456",
    )


# ── Spy adapter: proves the wiring (Step 1/RED target) ──────────────────────


@pytest.mark.asyncio
async def test_nonstream_applies_adapter_to_request_and_response(db, monkeypatch):
    model = make_model(db, name="adapter-llm")
    payload = {
        "choices": [{"message": {"role": "assistant", "content": "hi"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    _patch_client(monkeypatch, payload)
    spy = _install_spy(monkeypatch)

    result = await _call(model)

    assert spy.calls == ["timeout", "req", "resp"]
    assert spy.kinds == {"timeout": "chat", "req": "chat", "resp": "chat"}
    # The adapter's transformed response is what actually comes back — not
    # just called-and-ignored.
    assert result["_adapted_response"] is True


@pytest.mark.asyncio
async def test_nonstream_applies_adapter_error_on_4xx(db, monkeypatch):
    model = make_model(db, name="adapter-llm-4xx")
    _patch_client(monkeypatch, {"detail": "nope"}, status_code=400)
    spy = _install_spy(monkeypatch)

    with pytest.raises(HTTPException) as excinfo:
        await _call(model)

    assert spy.calls == ["timeout", "req", "err"]
    assert spy.kinds["err"] == "chat"
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail.startswith("adapted:")


@pytest.mark.asyncio
async def test_nonstream_applies_adapter_embeddings_kind(db, monkeypatch):
    model = make_model(db, name="adapter-embed")
    model.model_type = "embedding"
    db.commit()
    payload = {
        "data": [{"embedding": [0.1, 0.2]}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
    }
    _patch_client(monkeypatch, payload)
    spy = _install_spy(monkeypatch)

    result = await _call(
        model,
        request_body={"model": model.name, "input": ["hi"]},
        endpoint_path="/v1/embeddings",
    )

    assert spy.kinds == {"timeout": "embeddings", "req": "embeddings", "resp": "embeddings"}
    assert result["_adapted_response"] is True


# ── Passthrough (openai_compatible): must stay a no-op ──────────────────────


@pytest.mark.asyncio
async def test_openai_compatible_passthrough_is_noop(db, monkeypatch):
    """No ``get_adapter`` monkeypatch here — exercises the real registry.
    ``make_model`` defaults ``protocol`` to ``openai_compatible``, which
    resolves to ``PassthroughAdapter``; every hook is an identity/no-op for
    well-formed input, so the returned result must be exactly what the
    pre-adapter code would have produced (result JSON plus the injected
    ``anila_meta``, nothing else added/changed)."""
    model = make_model(db, name="passthrough-llm")
    assert model.protocol == "openai_compatible"
    payload = {
        "choices": [{"message": {"role": "assistant", "content": "hi"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }
    _patch_client(monkeypatch, payload)

    result = await _call(model)

    assert "_adapted_request" not in result
    assert "_adapted_response" not in result
    assert result["choices"] == payload["choices"]
    assert result["usage"] == payload["usage"]
    assert "anila_meta" in result  # injected by the (unchanged) success path


@pytest.mark.asyncio
async def test_openai_compatible_passthrough_error_detail_unchanged(db, monkeypatch):
    """4xx with a well-formed OpenAI-shape error body: the sanitized
    ``detail`` is exactly the upstream ``error.message`` — same extraction
    the pre-adapter code performed directly off ``response.json()``."""
    model = make_model(db, name="passthrough-llm-4xx")
    _patch_client(
        monkeypatch,
        {"error": {"message": "invalid model"}},
        status_code=400,
    )

    with pytest.raises(HTTPException) as excinfo:
        await _call(model)

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "invalid model"


@pytest.mark.asyncio
async def test_openai_compatible_passthrough_error_non_openai_shape_never_leaks(db, monkeypatch):
    """安全鎖：上游 4xx 回傳非 OpenAI-shape body（如 ``{"detail": ...}`` 或
    純文字內部 trace），經真正的 openai_compatible/PassthroughAdapter 路徑後,
    client 看到的 ``HTTPException.detail`` 必須是泛用訊息，絕對不含上游原文
    ——防止未來又把 base.py 的 fail-safe 改壞、重新洩漏上游內部細節。"""
    model = make_model(db, name="passthrough-llm-4xx-nonshape")
    upstream_secret = "invalid api key for upstream backend at internal-host:9999"
    _patch_client(
        monkeypatch,
        {"detail": upstream_secret},
        status_code=400,
    )

    with pytest.raises(HTTPException) as excinfo:
        await _call(model)

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "模型服務拒絕請求 (HTTP 400)"
    assert upstream_secret not in excinfo.value.detail


@pytest.mark.asyncio
async def test_unknown_protocol_falls_back_to_passthrough(db, monkeypatch):
    """A stale/unknown ``protocol`` DB value (e.g. a removed custom
    adapter) must not break the call — ``get_adapter`` fallback keeps it on
    the passthrough no-op path."""
    model = make_model(db, name="unknown-protocol-llm")
    model.protocol = "some_removed_custom_adapter"
    db.commit()
    payload = {
        "choices": [{"message": {"role": "assistant", "content": "hi"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    _patch_client(monkeypatch, payload)

    result = await _call(model)

    assert result["choices"] == payload["choices"]


# ── Task 6: streaming path (proxy_stream / _proxy_stream_impl) ─────────────
#
# Same contract as the non-stream suite above, applied to the SSE path:
# ``_proxy_stream_impl`` resolves a ``BackendAdapter`` from a new
# ``protocol`` parameter and applies ``to_backend_request`` to the outbound
# body, ``request_timeout`` to the client timeout, and
# ``from_backend_stream_chunk`` to every raw line read off
# ``resp.aiter_lines()`` *before* it enters the existing block-assembly /
# ``[DONE]`` holdback / ``anila.meta`` / usage-interception logic. A line
# that the adapter maps to ``None`` is dropped outright (never reaches
# ``block_lines``). ``openai_compatible`` (``PassthroughAdapter``) returns
# every raw line unchanged, so this suite locks byte-identical output for
# that path.


class _FakeStreamResponse:
    def __init__(self, lines: list[str], status_code: int = 200):
        self._lines = lines
        self.status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamClient:
    """Fake httpx.AsyncClient exposing only ``.stream(...)`` — same shape as
    test_proxy_stream_usage.py's ``_FakeAsyncClient``, plus call recording so
    tests can assert on the outbound (adapter-transformed) request body."""

    def __init__(self, lines: list[str], calls: list[dict], *args, **kwargs):
        self._lines = lines
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method: str, url: str, json: dict | None = None, headers=None):
        self._calls.append({"method": method, "url": url, "json": json, "headers": headers})
        return _FakeStreamResponse(self._lines)


def _patch_stream_client(monkeypatch, lines: list[str]) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr(
        proxy_impl.httpx,
        "AsyncClient",
        lambda *a, **k: _FakeStreamClient(lines, calls, *a, **k),
    )
    return calls


async def _collect_stream(**kwargs) -> list[str]:
    chunks = []
    async for chunk in proxy_impl.proxy_stream(**kwargs):
        chunks.append(chunk)
    return chunks


def _base_stream_kwargs(**overrides) -> dict:
    kwargs = dict(
        target_url="http://mock-llm/v1/chat/completions",
        api_key_id=1,
        user_id=2,
        department_id=None,
        usage_model_id=3,
        request_body={
            "model": "google/gemma4",
            "messages": [{"role": "user", "content": "hi"}],
        },
        model_name="google/gemma4",
    )
    kwargs.update(overrides)
    return kwargs


@pytest.mark.asyncio
async def test_stream_hook_passthrough_is_byte_identical(monkeypatch):
    """passthrough(openai_compatible)串流輸出與未接 hook 前逐位元組相同。

    Upstream 已自帶 usage 與 anila.meta,讓 ``_proxy_stream_impl`` 不需合成
    任何衍生內容(無 estimate usage、無 synthetic anila.meta)——整條輸出因
    此完全由『逐行 hook 是否改動內容』決定,不受 wall-clock
    (trace_id/latency_ms)影響,可以放心做逐位元組相等判斷。
    """
    lines = [
        'data: {"choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}',
        "",
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":11,"completion_tokens":7,"total_tokens":18}}',
        "",
        "event: anila.meta",
        'data: {"trace_id":"trace-1","trace":[],"citations":[],"confidence":null,'
        '"handoff_chain":[],"follow_ups":[],"latency_ms":3,"classified":false,"usage":null}',
        "",
        "data: [DONE]",
        "",
    ]
    _patch_stream_client(monkeypatch, lines)

    chunks = await _collect_stream(**_base_stream_kwargs(protocol="openai_compatible"))

    expected = [
        lines[0] + "\n\n",
        lines[2] + "\n\n",
        "\n".join(lines[4:6]) + "\n\n",
        lines[7] + "\n\n",
    ]
    assert chunks == expected


@pytest.mark.asyncio
async def test_stream_drop_line_when_adapter_returns_none(monkeypatch):
    """``adapter.from_backend_stream_chunk`` 回 ``None`` 的行(如 SSE
    comment/keepalive)要在組 block 之前就被丟棄,絕不能出現在任何輸出
    chunk 裡。"""

    class _DropCommentsAdapter:
        name = "openai_compatible"

        def request_timeout(self, endpoint_kind, default):
            return default

        def to_backend_request(self, endpoint_kind, body):
            return body

        def from_backend_stream_chunk(self, endpoint_kind, raw_line):
            return None if raw_line.startswith(":") else raw_line

    monkeypatch.setattr(
        proxy_impl, "get_adapter", lambda protocol: _DropCommentsAdapter()
    )

    lines = [
        ": keepalive",
        "",
        'data: {"choices":[{"index":0,"delta":{"content":"Hi"},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}',
        "",
        "data: [DONE]",
        "",
    ]
    _patch_stream_client(monkeypatch, lines)

    chunks = await _collect_stream(**_base_stream_kwargs(protocol="anything"))

    joined = "".join(chunks)
    assert "keepalive" not in joined
    assert lines[2] + "\n\n" in chunks
    assert lines[4] + "\n\n" in chunks


@pytest.mark.asyncio
async def test_stream_applies_adapter_to_request_body_and_timeout(monkeypatch):
    """順序驗證:先套 ``adapter.to_backend_request`` 再強制注入
    ``stream``/``stream_options`` ——adapter 的轉換結果必須留在最終送上游
    的 body 裡,且 ``stream_options.include_usage`` 一定存在(既有行為,不因
    換了 adapter 而消失)。timeout 改走 ``adapter.request_timeout("chat",
    default)``。"""
    spy = _install_spy(monkeypatch)
    lines = ["data: [DONE]", ""]
    calls = _patch_stream_client(monkeypatch, lines)

    await _collect_stream(**_base_stream_kwargs(protocol="spy_protocol"))

    assert "req" in spy.calls
    assert "timeout" in spy.calls
    assert spy.kinds.get("req") == "chat"
    assert spy.kinds.get("timeout") == "chat"

    assert calls, "client.stream() 應該被呼叫一次"
    sent_body = calls[0]["json"]
    assert sent_body["_adapted_request"] is True
    assert sent_body["stream"] is True
    assert sent_body["stream_options"] == {"include_usage": True}
