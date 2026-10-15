"""Real proxy accounting with synthetic HTTP and opt-in completion metadata."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.services import internal_llm, quick_model_stream
from app.services.completion_metadata import completion_metadata, normalize_completion
from app.services.internal_llm import InternalCompletionError
from app.services.proxy import service as proxy
from tests.test_quick_model_stream import frame, gateway as _gateway
from tests.test_quick_stream_terminal import finish

gateway = _gateway


async def collect(db, gateway, *, return_metadata=True, receive=None):
    seen = []

    async def delta(text):
        seen.append(text)
        if receive:
            await receive(text)

    result = await quick_model_stream.stream_chat(
        db, gateway.model, {"messages": [{"role": "user", "content": "合成問題"}]},
        user_id=gateway.user.id, department_id=7, caller_agent_id=9, api_key_id=11,
        on_delta=delta, before_chunk=lambda: None, return_metadata=return_metadata,
    )
    return result, seen


@pytest.mark.parametrize("reason", ["stop", "length", "content_filter", "tool_calls", "function_call"])
def test_stream_preserves_finish_and_safe_usage_without_double_accounting(db, gateway, reason):
    gateway.chunks = [
        frame("部分合成回答", usage={"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12, "reasoning_tokens": 2}),
        finish(reason), "data: [DONE]\n\n",
    ]
    result, seen = asyncio.run(collect(db, gateway))
    assert result["text"] == "".join(seen) == "部分合成回答"
    assert result["completion"] == completion_metadata(
        finish_reason=reason, terminal_source="finish_reason",
        usage={"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12, "reasoning_tokens": 2},
    )
    assert normalize_completion(result) == result
    assert result["completion"]["usage_complete"] is True
    assert len(gateway.normal) == 1 and gateway.partial == [] and gateway.closed == 1
    assert gateway.normal[0]["token_source"] == "reported"
    assert gateway.normal[0]["api_key_id"] == 11


def test_done_only_preserves_none_reason_and_estimated_usage(db, gateway):
    result, seen = asyncio.run(collect(db, gateway))
    meta = result["completion"]
    assert result["text"] == "".join(seen) == "合成回答"
    assert meta["terminal_source"] == "done" and meta["finish_reason"] is None
    assert meta["answer_complete"] is True and meta["usage_complete"] is True
    assert meta["usage"]["total_tokens"] > 0
    assert len(gateway.normal) == 1 and gateway.normal[0]["token_source"] == "estimated"


@pytest.mark.parametrize("reason", ["length", "content_filter", "tool_calls", "function_call"])
def test_empty_incomplete_stream_is_available_only_in_metadata_mode(db, gateway, reason):
    gateway.chunks = [finish(reason)]
    result, seen = asyncio.run(collect(db, gateway))
    assert result["text"] == "" and seen == []
    assert result["completion"]["answer_complete"] is False
    assert result["completion"]["usage_complete"] is True
    assert len(gateway.normal) == 1 and gateway.partial == []


def test_legacy_empty_filtered_stream_keeps_original_error_contract(db, gateway):
    gateway.chunks = [finish("content_filter")]
    with pytest.raises(InternalCompletionError) as error:
        asyncio.run(collect(db, gateway, return_metadata=False))
    assert error.value.status_code is None
    assert len(gateway.normal) == 1 and gateway.partial == []


@pytest.mark.parametrize("mode", [False, True])
@pytest.mark.parametrize("reasons", [("length", "stop"), ("stop", "length"), ("content_filter", "stop")])
def test_conflicting_reasons_cannot_wash_an_incomplete_finish(db, gateway, mode, reasons):
    gateway.chunks = [frame("部分"), *map(finish, reasons)]
    with pytest.raises(InternalCompletionError) as error:
        asyncio.run(collect(db, gateway, return_metadata=mode))
    assert error.value.status_code == 502
    assert gateway.normal == [] and len(gateway.partial) == 1 and gateway.closed == 1


def test_repeated_identical_reason_is_not_a_conflict(db, gateway):
    gateway.chunks = [frame("部分"), finish("length"), finish("length")]
    result, _ = asyncio.run(collect(db, gateway))
    assert result["completion"]["type"] == "MODEL_OUTPUT_LIMIT"
    assert len(gateway.normal) == 1 and gateway.partial == []


def test_metadata_mode_does_not_upgrade_bare_eof_to_completion(db, gateway):
    gateway.chunks = [frame("部分", usage={"prompt_tokens": 8, "completion_tokens": 4})]
    with pytest.raises(InternalCompletionError):
        asyncio.run(collect(db, gateway))
    assert gateway.normal == [] and len(gateway.partial) == 1
    assert gateway.partial[0]["outcome"] == "partial"


def test_metadata_mode_cancel_still_closes_and_accounts_partial_once(db, gateway):
    gateway.chunks, gateway.park = [frame("部分")], True

    async def scenario():
        emitted = asyncio.Event()

        async def receive(_text):
            emitted.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(collect(db, gateway, receive=receive))
        await emitted.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert gateway.closed == 1 and gateway.normal == [] and len(gateway.partial) == 1


def test_named_usage_after_done_is_drained_without_provider_text(db, gateway, monkeypatch):
    drained = []

    async def upstream(**kwargs):
        try:
            yield frame("可見")
            yield "data: [DONE]\n\n"
            yield 'event: anila.meta\ndata: {"usage":{"total_tokens":7,"provider":"DO_NOT_FORWARD"},"message":"DO_NOT_FORWARD"}\n\n'
            yield frame("不能外傳的終止後資料")
        finally:
            drained.append(True)

    monkeypatch.setattr(proxy, "proxy_stream", upstream)
    result, seen = asyncio.run(collect(db, gateway))
    assert result["text"] == "".join(seen) == "可見"
    assert result["completion"]["usage"] == {"total_tokens": 7}
    assert "DO_NOT_FORWARD" not in json.dumps(result) and drained == [True]


def install_nonstream(monkeypatch, gateway, reason, *, text="部分合成回答"):
    payload = {
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": reason}],
        "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12, "reasoning_tokens": 2, "provider": "DO_NOT_FORWARD"},
    }

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            gateway.requests.append({"url": url, **kwargs})
            return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr(proxy.httpx, "AsyncClient", Client)
    monkeypatch.setattr(proxy, "resolve_model_gateway_key", lambda _model: "synthetic-test-key")


async def nonstream(db, gateway, **kwargs):
    return await internal_llm.complete_chat(
        db, gateway.model, {"messages": [{"role": "user", "content": "合成問題"}]},
        user_id=gateway.user.id, department_id=7, api_key_id=11,
        caller_agent_id=9, on_behalf_of_user=True, **kwargs,
    )


@pytest.mark.parametrize("reason", [None, "stop", "length", "content_filter", "tool_calls", "function_call"])
def test_nonstream_metadata_preserves_reason_and_real_proxy_accounting(db, gateway, monkeypatch, reason):
    install_nonstream(monkeypatch, gateway, reason)
    result = asyncio.run(nonstream(db, gateway, return_metadata=True))
    assert result == {"text": "部分合成回答", "completion": completion_metadata(
        finish_reason=reason, terminal_source="response",
        usage={"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12, "reasoning_tokens": 2},
    )}
    assert "DO_NOT_FORWARD" not in json.dumps(result)
    assert len(gateway.quota) == len(gateway.normal) == 1 and gateway.partial == []
    assert gateway.normal[0]["api_key_id"] == 11
    assert gateway.normal[0]["token_source"] == "reported"


def test_nonstream_metadata_can_report_empty_filter_but_legacy_still_raises(db, gateway, monkeypatch):
    install_nonstream(monkeypatch, gateway, "content_filter", text="")
    result = asyncio.run(nonstream(db, gateway, return_metadata=True))
    assert result["text"] == "" and result["completion"]["type"] == "MODEL_CONTENT_FILTER"
    with pytest.raises(InternalCompletionError) as error:
        asyncio.run(nonstream(db, gateway))
    assert error.value.status_code is None


def test_nonstream_default_still_returns_plain_text(db, gateway, monkeypatch):
    install_nonstream(monkeypatch, gateway, "length")
    assert asyncio.run(nonstream(db, gateway)) == "部分合成回答"


@pytest.mark.parametrize("reason", ["provider_reason", True, {}])
def test_nonstream_metadata_rejects_unknown_finish_without_echo(db, gateway, monkeypatch, reason):
    install_nonstream(monkeypatch, gateway, reason)
    with pytest.raises(InternalCompletionError) as error:
        asyncio.run(nonstream(db, gateway, return_metadata=True))
    assert error.value.status_code == 502
    assert str(error.value) == "內部模型呼叫失敗"
