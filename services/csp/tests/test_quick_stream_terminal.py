"""Explicit upstream terminal proof, real proxy accounting and legacy opt-in."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.services.internal_llm import InternalCompletionError
from app.services.proxy import service as proxy
from tests.test_quick_model_stream import collect, frame, gateway as _gateway

gateway = _gateway


async def raw_proxy(gateway, *, strict=True):
    return [chunk async for chunk in proxy.proxy_stream(
        target_url="https://model.invalid/v2/chat/completions", api_key_id=11,
        user_id=gateway.user.id, department_id=7, usage_model_id=gateway.model.id,
        request_body={"model": gateway.model.name, "messages": []},
        model_name=gateway.model.name, gateway_api_key="synthetic-gateway-token",
        tuning=proxy.ProxyTuning.from_registry_defaults(), require_stream_terminal=strict,
    )]


def finish(reason="stop"):
    return "data: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}) + "\n\n"


@pytest.mark.parametrize("reason", ["stop", "length", "content_filter", "tool_calls", "function_call"])
def test_finish_reason_is_sufficient_without_fabricated_done(db, gateway, reason):
    gateway.chunks = [frame("完整合成回答"), finish(reason)]
    text, seen = asyncio.run(collect(db, gateway))
    assert text == "完整合成回答" and "".join(seen) == text
    assert len(gateway.normal) == 1 and gateway.partial == []
    assert gateway.closed == 1


def test_strict_proxy_finish_only_preserves_terminal_and_usage_once(db, gateway):
    gateway.chunks = [frame("完整回答"), finish("stop")]
    chunks = asyncio.run(raw_proxy(gateway))
    assert any('"finish_reason": "stop"' in chunk for chunk in chunks)
    assert not any("[DONE]" in chunk for chunk in chunks)
    assert any('"usage_complete":true' in chunk for chunk in chunks)
    assert len(gateway.normal) == 1 and gateway.partial == []


@pytest.mark.parametrize("terminal", [None, "event: unrelated\ndata: [DONE]\n\n", finish(""), finish(None)])
def test_strict_proxy_clean_eof_never_publishes_success_or_done(db, gateway, terminal):
    gateway.chunks = [frame("合成部分回答", usage={"prompt_tokens": 8, "completion_tokens": 4})]
    if terminal:
        gateway.chunks.append(terminal)
    chunks = asyncio.run(raw_proxy(gateway))
    joined = "".join(chunks)
    assert "anila.error" in joined
    expected = "UPSTREAM_STREAM_INVALID_TERMINAL" if terminal == finish("") else "UPSTREAM_STREAM_INCOMPLETE"
    assert expected in joined
    assert '"usage_complete":true' not in joined
    # An unrelated named event is not itself an OpenAI termination frame.
    if terminal is None:
        assert "data: [DONE]\n\n" not in joined
    assert gateway.normal == [] and len(gateway.partial) == 1
    assert gateway.partial[0]["outcome"] == "partial"
    assert gateway.partial[0]["prompt_tokens"] == 8
    assert gateway.partial[0]["completion_tokens"] == 4
    assert gateway.partial[0]["token_source"] == "reported"


def test_adapter_independently_rejects_replaced_proxy_without_terminal(db, gateway, monkeypatch):
    async def upstream(**kwargs):
        assert kwargs["require_stream_terminal"] is True
        yield frame("合成部分回答")
    monkeypatch.setattr(proxy, "proxy_stream", upstream)
    with pytest.raises(InternalCompletionError):
        asyncio.run(collect(db, gateway))


def test_default_proxy_retains_legacy_eof_contract(db, gateway):
    gateway.chunks = [frame("既有非標準上游的合成回答")]
    chunks = asyncio.run(raw_proxy(gateway, strict=False))
    assert "anila.error" not in "".join(chunks)
    assert any('"usage_complete":true' in chunk for chunk in chunks)
    assert len(gateway.normal) == 1 and gateway.partial == []


def test_named_usage_meta_without_terminal_stays_partial(db, gateway):
    gateway.chunks = [frame("部分回答"), 'event: anila.meta\ndata: {"usage_complete":true}\n\n']
    chunks = asyncio.run(raw_proxy(gateway))
    joined = "".join(chunks)
    assert '"usage_complete":false' in joined and '"usage_complete":true' not in joined
    assert "anila.error" in joined
    assert gateway.normal == [] and len(gateway.partial) == 1


@pytest.mark.parametrize("reason", ["unknown", "", " STOP ", True, 1, {}, []])
@pytest.mark.parametrize("done", [False, True])
def test_invalid_finish_reason_rejected_even_with_done(db, gateway, reason, done):
    gateway.chunks = [frame("部分回答"), finish(reason)]
    if done:
        gateway.chunks.append("data: [DONE]\n\n")
    with pytest.raises(InternalCompletionError):
        asyncio.run(collect(db, gateway))
    assert gateway.normal == [] and len(gateway.partial) == 1
    assert gateway.partial[0]["outcome"] == "partial"


@pytest.mark.parametrize("reason", ["unknown", "", 1, {}])
def test_strict_proxy_invalid_reason_has_partial_error_once(db, gateway, reason):
    gateway.chunks = [frame("部分回答"), finish(reason), "data: [DONE]\n\n"]
    joined = "".join(asyncio.run(raw_proxy(gateway)))
    assert "UPSTREAM_STREAM_INVALID_TERMINAL" in joined
    assert "data: [DONE]\n\n" not in joined
    assert gateway.normal == [] and len(gateway.partial) == 1
