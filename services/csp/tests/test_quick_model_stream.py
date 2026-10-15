"""終端模型串流的合成 HTTP、逐片撤權、取消與唯一用量結算。"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from app.models.model_registry import ModelRegistry
from app.services import quick_model_stream as stream
from app.services import usage_writer
from app.services.internal_llm import InternalCompletionError
from app.services.proxy import headers, service as proxy
from app.services import quota_service
from tests.conftest import make_user


def frame(content=None, *, delta=None, usage=None):
    payload = {"choices": [{"index": 0, "delta": delta or {"content": content}}]}
    if usage is not None:
        payload["usage"] = usage
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


@pytest.fixture
def gateway(db, monkeypatch):
    user = make_user(db, "stream-caller")
    model = ModelRegistry(
        name="synthetic-terminal-model",
        display_name="合成終端模型",
        model_type="llm",
        endpoint_url="https://model.invalid/gateway/v1",
        api_version="v2",
        is_active=True,
        temperature=0.17,
        top_p=0.77,
    )
    db.add(model)
    db.commit()
    state = SimpleNamespace(
        user=user,
        model=model,
        requests=[],
        guards=[],
        quota=[],
        normal=[],
        partial=[],
        closed=0,
        key_models=[],
        chunks=[frame("合成"), frame("回答"), "data: [DONE]\n\n"],
        fail=None,
        close_fail=None,
        park=False,
    )

    class ResponseStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in state.chunks:
                yield chunk.encode("utf-8")
            if state.fail:
                raise state.fail
            if state.park:
                await asyncio.Event().wait()

        async def aclose(self):
            state.closed += 1
            if state.close_fail:
                raise state.close_fail

    def reply(request):
        state.requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ResponseStream(),
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        proxy.httpx,
        "AsyncClient",
        lambda *args, **kwargs: real_client(
            *args, transport=httpx.MockTransport(reply), **kwargs
        ),
    )
    monkeypatch.setattr(
        proxy, "_guard_outbound", lambda url, **kw: state.guards.append((url, kw))
    )
    monkeypatch.setattr(proxy, "_note_proxy_outcome", lambda **kw: None)
    monkeypatch.setattr(
        proxy, "_estimate_token_count", lambda _model, text: max(1, len(text))
    )
    monkeypatch.setattr(
        quota_service, "enforce_call_quota", lambda _db, **kw: state.quota.append(kw)
    )

    def resolve_key(model):
        state.key_models.append(model)
        return "synthetic-gateway-token"

    monkeypatch.setattr(headers, "resolve_model_gateway_key", resolve_key)

    async def enqueue(**kwargs):
        state.normal.append(kwargs)

    monkeypatch.setattr(proxy, "enqueue_usage", enqueue)
    monkeypatch.setattr(
        usage_writer, "queue_usage_nowait", lambda row: state.partial.append(dict(row))
    )
    return state


async def collect(db, gateway, *, before_chunk=lambda: None, on_delta=None):
    seen = []

    async def delta(text):
        seen.append(text)
        if on_delta:
            await on_delta(text)

    text = await stream.stream_chat(
        db,
        gateway.model,
        {"messages": [{"role": "user", "content": "合成提問"}], "temperature": 0.99},
        user_id=gateway.user.id,
        department_id=7,
        caller_agent_id=9,
        api_key_id=11,
        on_delta=delta,
        before_chunk=before_chunk,
    )
    return text, seen


def test_real_proxy_uses_snapshot_gateway_sampling_ssrf_and_actual_usage(db, gateway):
    gateway.chunks = [
        frame("合成"),
        frame(
            "回答",
            usage={"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
        ),
        "data: [DONE]\n\n",
    ]
    text, seen = asyncio.run(collect(db, gateway))
    assert text == "合成回答" and seen == ["合成", "回答"]
    request = gateway.requests[0]
    assert str(request.url) == "https://model.invalid/gateway/v2/chat/completions"
    assert request.headers["authorization"] == "Bearer synthetic-gateway-token"
    assert "x-anila-dispatch-jwt" not in request.headers
    payload = json.loads(request.content)
    assert payload["model"] == gateway.model.name
    assert payload["temperature"] == 0.99
    assert payload["top_p"] == 0.77
    assert (
        payload["stream"] is True and payload["stream_options"]["include_usage"] is True
    )
    assert gateway.key_models[0] is not gateway.model
    assert len(gateway.guards) == len(gateway.quota) == len(gateway.normal) == 1
    quota, usage = gateway.quota[0], gateway.normal[0]
    assert quota["user_id"] == usage["user_id"] == gateway.user.id
    assert quota["api_key_id"] == usage["api_key_id"] == 11
    assert quota["usage_kind"] == usage["usage_kind"] == "inference"
    assert usage["caller_agent_id"] == 9 and usage["department_id"] == 7
    assert usage["request_type"] == "chat" and usage["token_source"] == "reported"
    assert usage["total_tokens"] == 14
    assert gateway.partial == [] and gateway.closed == 1


def test_reasoning_tool_calls_and_fragmented_inline_think_never_reach_deltas(
    db, gateway
):
    gateway.chunks = [
        frame(
            delta={
                "reasoning_content": "hidden reasoning",
                "tool_calls": [{"id": "private"}],
            }
        ),
        frame("<thi"),
        frame("nk>hidden inline"),
        frame("</think>可見"),
        'event: anila.reasoning\ndata: {"text":"hidden event"}\n\n',
        frame("回答"),
        "data: [DONE]\n\n",
    ]
    text, seen = asyncio.run(collect(db, gateway))
    assert text == "可見回答" and "".join(seen) == text
    assert "hidden" not in text and "private" not in text
    assert len(gateway.normal) == 1 and gateway.partial == []


def test_adapter_parses_fragmented_crlf_and_drains_usage_after_done(
    db, gateway, monkeypatch
):
    chunks = (frame("中文") + frame("回答")).replace("\n", "\r\n")
    pieces = list(chunks)
    drained = []

    async def upstream(**kwargs):
        try:
            for piece in pieces:
                yield piece
            yield "data: [DONE]\n\n"
            drained.append("usage")
            yield frame("應忽略的終止後資料")
        finally:
            drained.append("closed")

    monkeypatch.setattr(proxy, "proxy_stream", upstream)
    text, seen = asyncio.run(collect(db, gateway))
    assert text == "中文回答" and "".join(seen) == text
    assert drained == ["usage", "closed"]


def test_quota_preflight_failure_never_opens_upstream(db, gateway, monkeypatch):
    def denied(*args, **kwargs):
        raise HTTPException(
            429, detail={"code": "quota_exceeded", "message": "合成額度已用完"}
        )

    monkeypatch.setattr(quota_service, "enforce_call_quota", denied)
    with pytest.raises(InternalCompletionError) as denied:
        asyncio.run(collect(db, gateway))
    assert denied.value.status_code == 429
    assert denied.value.quota_code == "quota_exceeded"
    assert denied.value.quota_message == "合成額度已用完"
    assert gateway.requests == [] and gateway.normal == [] and gateway.partial == []


def test_before_chunk_401_stops_delivery_and_closes_partial_usage(db, gateway):
    checks = 0
    seen = []

    def authorize():
        nonlocal checks
        checks += 1
        if checks == 3:
            raise HTTPException(401, detail="synthetic identity changed")

    async def receive(text):
        seen.append(text)

    async def scenario():
        with pytest.raises(HTTPException) as denied:
            await collect(db, gateway, before_chunk=authorize, on_delta=receive)
        assert denied.value.status_code == 401
        assert gateway.closed == 1
        assert len(gateway.partial) == 1

    asyncio.run(scenario())
    assert seen == ["合成"] and gateway.normal == []
    assert gateway.partial[0]["outcome"] == "partial"


@pytest.mark.parametrize("phase", ["consumer", "upstream", "failing_cleanup"])
def test_consumer_cancel_closes_proxy_and_records_partial_exactly_once(
    db, gateway, phase
):
    emitted = asyncio.Event()
    seen = []
    gateway.chunks = [frame("合成")]
    gateway.park = True
    if phase == "failing_cleanup":
        gateway.close_fail = httpx.ReadError("synthetic cleanup failure")

    async def receive(text):
        seen.append(text)
        emitted.set()
        if phase != "upstream":
            await asyncio.Event().wait()

    async def scenario():
        task = asyncio.create_task(collect(db, gateway, on_delta=receive))
        await emitted.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert gateway.closed == 1
        assert len(gateway.partial) == 1

    asyncio.run(scenario())
    assert seen == ["合成"] and gateway.normal == []
    assert gateway.partial[0]["user_id"] == gateway.user.id
    assert gateway.partial[0]["api_key_id"] == 11
    assert gateway.partial[0]["request_type"] == "chat"
    assert gateway.partial[0]["total_tokens"] > 0


@pytest.mark.parametrize("reported", [False, True])
def test_upstream_failure_is_not_success_and_records_partial_once(
    db, gateway, reported
):
    usage = (
        {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12}
        if reported
        else None
    )
    gateway.chunks = [frame("部分回答", usage=usage)]
    gateway.fail = httpx.ReadError("synthetic closed upstream")
    with pytest.raises(InternalCompletionError):
        asyncio.run(collect(db, gateway))
    assert gateway.closed == 1 and gateway.normal == []
    assert len(gateway.partial) == 1
    assert gateway.partial[0]["outcome"] == "partial"
    if reported:
        assert gateway.partial[0]["total_tokens"] == 12
        assert gateway.partial[0]["token_source"] == "reported"


@pytest.mark.parametrize("last_frame", ["anila.meta", "[DONE]"])
def test_public_proxy_close_at_terminal_frame_records_one_partial(
    db, gateway, last_frame
):
    gateway.chunks = [
        frame(
            "回答", usage={"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12}
        ),
        "data: [DONE]\n\n",
    ]

    async def scenario():
        upstream = proxy.proxy_stream(
            target_url="https://model.invalid/v2/chat/completions",
            api_key_id=11,
            user_id=gateway.user.id,
            department_id=7,
            usage_model_id=gateway.model.id,
            request_body={"model": gateway.model.name, "messages": []},
            model_name=gateway.model.name,
            gateway_api_key="synthetic-gateway-token",
            tuning=proxy.ProxyTuning.from_registry_defaults(),
        )
        async for chunk in upstream:
            if last_frame in chunk:
                await upstream.aclose()
                break
        assert gateway.closed == 1
        assert len(gateway.partial) == 1

    asyncio.run(scenario())
    assert gateway.normal == []
    assert gateway.partial[0]["total_tokens"] == 12
    assert gateway.partial[0]["token_source"] == "reported"


def test_real_model_concurrency_slot_is_held_and_released(db, gateway, monkeypatch):
    from app.services import model_gate

    board = model_gate.MemoryBoard()
    monkeypatch.setattr(model_gate, "get_board", lambda: board)
    gateway.model.max_concurrent = 1
    db.commit()
    held = []

    async def receive(text):
        held.append(len(board.states[str(gateway.model.id)]["inflight"]))

    asyncio.run(collect(db, gateway, on_delta=receive))
    assert held == [1, 1]
    assert board.states[str(gateway.model.id)]["inflight"] == {}
    assert len(gateway.normal) == 1 and gateway.partial == []


@pytest.mark.parametrize("content,success", [("x" * 32000, True), ("x" * 32001, False)])
def test_content_bound_matches_graph_exactly(db, gateway, content, success):
    gateway.chunks = [frame(content), "data: [DONE]\n\n"]
    if success:
        text, seen = asyncio.run(collect(db, gateway))
        assert text == content and "".join(seen) == content
        assert len(gateway.normal) == 1 and gateway.partial == []
    else:
        with pytest.raises(InternalCompletionError):
            asyncio.run(collect(db, gateway))
        assert gateway.normal == [] and len(gateway.partial) == 1


@pytest.mark.parametrize(
    "chunk",
    [
        'event: anila.error\ndata: {"message":"private upstream details"}\n\n',
        "data: {invalid}\n\n",
        'data: {"error":{"message":"private"}}\n\n',
        "data: [DONE]\n\n",
    ],
)
def test_error_malformed_and_empty_streams_fail_without_leaking_details(
    db, gateway, chunk
):
    gateway.chunks = [chunk]
    with pytest.raises(InternalCompletionError) as failed:
        asyncio.run(collect(db, gateway))
    assert "private" not in str(failed.value)
    assert gateway.closed == 1
