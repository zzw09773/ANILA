"""service_wrapper 串流：emit 的 SSE 必須與 CSP Router 的解析相容。

把 _sse_stream 的輸出，用「Router 端 proxy_service.proxy_stream 一樣的方式」解析：
空行分塊 → 抽 ``data:`` 行 → json.loads → 讀 choices[].delta.content / usage / [DONE]。
這證明格式對得上，不需要 live 模型。
"""

from __future__ import annotations

import json

import pytest

fastapi = pytest.importorskip("fastapi")  # serving extra；沒裝就跳過

from agents.stream_events import RawResponsesStreamEvent  # noqa: E402
from openai.types.responses import ResponseTextDeltaEvent  # noqa: E402

from anila_agent.serving import service_wrapper  # noqa: E402

pytestmark = pytest.mark.unit


def _delta_event(text: str) -> RawResponsesStreamEvent:
    data = ResponseTextDeltaEvent(
        content_index=0,
        delta=text,
        item_id="item-1",
        logprobs=[],
        output_index=0,
        sequence_number=0,
        type="response.output_text.delta",
    )
    return RawResponsesStreamEvent(data=data, type="raw_response_event")


class _FakeUsage:
    input_tokens = 11
    output_tokens = 7
    total_tokens = 18


class _FakeCtx:
    usage = _FakeUsage()


class _FakeStreaming:
    context_wrapper = _FakeCtx()

    def __init__(self, events):
        self._events = events

    async def stream_events(self):
        for ev in self._events:
            yield ev


def _parse_like_router(sse_text: str):
    """模仿 proxy_service.proxy_stream 的解析：空行分塊、抽 data:、讀 delta.content / usage。"""
    content: list[str] = []
    usage = None
    finish = None
    saw_done = False
    for block in sse_text.split("\n\n"):
        block = block.strip()
        if not block.startswith("data:"):
            continue
        data = block[len("data:") :].strip()
        if data == "[DONE]":
            saw_done = True
            continue
        chunk = json.loads(data)
        for choice in chunk.get("choices", []):
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
        if chunk.get("usage"):
            usage = chunk["usage"]
    return "".join(content), finish, usage, saw_done


async def _collect(agen) -> str:
    return "".join([s async for s in agen])


async def test_sse_roundtrip_router_compatible(monkeypatch):
    events = [_delta_event("你好"), _delta_event("，世界")]
    monkeypatch.setattr(service_wrapper, "run_streamed", lambda *a, **k: _FakeStreaming(events))

    sse = await _collect(service_wrapper._sse_stream(None, "hi", hooks=None))
    content, finish, usage, saw_done = _parse_like_router(sse)

    assert content == "你好，世界"  # 逐 delta 串接還原
    assert finish == "stop"
    assert usage == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    assert saw_done is True


async def test_first_block_role_and_chunk_object(monkeypatch):
    monkeypatch.setattr(
        service_wrapper, "run_streamed", lambda *a, **k: _FakeStreaming([_delta_event("x")])
    )
    sse = await _collect(service_wrapper._sse_stream(None, "hi", hooks=None))
    blocks = [b for b in sse.split("\n\n") if b.strip()]

    first = json.loads(blocks[0][len("data:") :].strip())
    assert first["object"] == "chat.completion.chunk"
    assert first["choices"][0]["delta"] == {"role": "assistant"}
    assert blocks[-1].strip() == "data: [DONE]"


async def test_stream_error_mid_flight_closes_cleanly(monkeypatch):
    class _Boom:
        context_wrapper = _FakeCtx()

        async def stream_events(self):
            yield _delta_event("partial")
            raise RuntimeError("model died mid-stream")

    monkeypatch.setattr(service_wrapper, "run_streamed", lambda *a, **k: _Boom())
    sse = await _collect(service_wrapper._sse_stream(None, "hi", hooks=None))
    content, finish, _usage, saw_done = _parse_like_router(sse)

    # 中途炸掉：仍拿到已串的部分 + 正常收尾（finish + [DONE]），不留半開連線。
    assert content == "partial"
    assert finish == "stop"
    assert saw_done is True


# ---- HTTP 端點層（TestClient）：證明 branch + content-type + auth 守衛 ----


async def _fake_dispatch_claims(_authorization=None, **_kwargs):
    return {"user_id": 1, "department": None, "agent_id": 9}


def _patch_guards(monkeypatch):
    monkeypatch.setattr(service_wrapper, "COLLECTION_ID", 12)
    monkeypatch.setattr(
        service_wrapper, "verify_dispatch_authorization", _fake_dispatch_claims
    )
    # In-task outbound auth = request-scoped dispatch JWT（不再要 csk-）。
    monkeypatch.setattr(service_wrapper, "build_model", lambda *a, **k: object())
    monkeypatch.setattr(service_wrapper, "build_agent", lambda *a, **k: object())


def test_http_stream_true_returns_event_stream(monkeypatch):
    from fastapi.testclient import TestClient

    _patch_guards(monkeypatch)
    monkeypatch.setattr(
        service_wrapper, "run_streamed", lambda *a, **k: _FakeStreaming([_delta_event("嗨")])
    )
    with TestClient(service_wrapper.app) as client:
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test"},
            json={"model": "anila-agent", "messages": [{"role": "user", "content": "hi"}],
                  "stream": True},
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    content, finish, _u, done = _parse_like_router(resp.text)
    assert content == "嗨"
    assert finish == "stop"
    assert done is True


def test_http_stream_false_returns_json(monkeypatch):
    from fastapi.testclient import TestClient

    class _Result:
        final_output = "純文字答案"

    async def _fake_run_once(*a, **k):
        return _Result()

    _patch_guards(monkeypatch)
    monkeypatch.setattr(service_wrapper, "run_once", _fake_run_once)
    with TestClient(service_wrapper.app) as client:
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test"},
            json={"model": "anila-agent", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "純文字答案"


def test_http_unauthorized_when_token_required(monkeypatch):
    from fastapi.testclient import TestClient

    # 預設 fail-closed：缺 Authorization Bearer → 401。
    monkeypatch.setattr(service_wrapper, "COLLECTION_ID", 12)
    monkeypatch.setattr(service_wrapper, "build_model", lambda *a, **k: object())
    with TestClient(service_wrapper.app) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "anila-agent", "messages": [{"role": "user", "content": "hi"}],
                  "stream": True},
        )
    assert resp.status_code == 401
