"""service-wrapper 端點契約（in-process TestClient，不連網）。

涵蓋：health 探針、/v1/models manifest（model_type=agent + created/owned_by）、
無 service token 時 /v1/chat/completions 的 401 fail-closed、非串流/串流的
usage 欄位、以及 OpenAI 多模態 array content（text parts 抽取 / 純圖片 422）。
"""

from __future__ import annotations

import importlib
import json

import pytest

pytest.importorskip("fastapi")
from agents.stream_events import RawResponsesStreamEvent
from fastapi.testclient import TestClient
from openai.types.responses import ResponseTextDeltaEvent

from anila_agent.serving import service_wrapper

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
    input_tokens = 3
    output_tokens = 2
    total_tokens = 5


class _FakeCtx:
    usage = _FakeUsage()


_DEFAULT_USAGE = object()  # sentinel：預設帶 _FakeUsage 真值


class _FakeStreaming:
    def __init__(self, events, usage=_DEFAULT_USAGE):
        self._events = events
        if usage is _DEFAULT_USAGE:
            self.context_wrapper = _FakeCtx()
        elif usage is not None:
            self.context_wrapper = type("_Ctx", (), {"usage": usage})()
        # usage=None → 不設 context_wrapper，模擬拿不到 SDK usage

    async def stream_events(self):
        for ev in self._events:
            yield ev


def _patch_guards(monkeypatch):
    """跟 test_streaming.py 一致的守衛放行組合：讓 chat_completions 走到
    build_agent/run_once|run_streamed，而不被 401/503 擋在門口。"""
    monkeypatch.setattr(service_wrapper, "COLLECTION_ID", 12)
    monkeypatch.setattr(service_wrapper, "ALLOW_NO_SERVICE_TOKEN", True)
    monkeypatch.setattr(service_wrapper, "CSP_SEARCH_TOKEN", "csk-test")
    monkeypatch.setattr(service_wrapper, "build_model", lambda *a, **k: object())
    monkeypatch.setattr(service_wrapper, "build_agent", lambda *a, **k: object())


@pytest.fixture
def client(monkeypatch):
    # 確保 import 時 token 為空、未 opt-out → 預期 401 fail-closed。
    monkeypatch.delenv("CSP_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("ANILA_ALLOW_NO_SERVICE_TOKEN", raising=False)
    from anila_agent.serving import service_wrapper

    importlib.reload(service_wrapper)  # 重新讀模組層級 env 全域
    with TestClient(service_wrapper.app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_models_manifest_marks_agent(client):
    r = client.get("/v1/models")
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "list"
    assert data["data"][0]["model_type"] == "agent"  # CSP 註冊標記


def test_chat_without_token_is_401(client):
    r = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 401  # fail-closed：未設 token 且未 opt-out


def test_chat_503_when_collection_id_unset(monkeypatch):
    # 認證放行（local-dev opt-out）但缺 ANILA_COLLECTION_ID → 明確 503，而非裸 ValueError 變 500。
    monkeypatch.delenv("CSP_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("ANILA_ALLOW_NO_SERVICE_TOKEN", "1")
    monkeypatch.delenv("ANILA_COLLECTION_ID", raising=False)
    from anila_agent.serving import service_wrapper

    importlib.reload(service_wrapper)
    with TestClient(service_wrapper.app) as c:
        r = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 503


# ---- /v1/models：OpenAI 規格必含 created/owned_by（model_type=agent 平台契約不動）----


def test_models_manifest_has_created_and_owned_by(client):
    r = client.get("/v1/models")
    assert r.status_code == 200
    entry = r.json()["data"][0]
    assert isinstance(entry["created"], int) and entry["created"] > 0
    assert entry["owned_by"] == "anila-agent"
    assert entry["model_type"] == "agent"  # 平台契約標記，勿改（見 code 註解）


# ---- 非串流：/v1/chat/completions 有真值才附 usage、未知時省略 -------------------


def test_http_stream_false_includes_real_usage_from_result(monkeypatch):
    class _Result:
        final_output = "答案"
        context_wrapper = _FakeCtx()

    async def _fake_run_once(*a, **k):
        return _Result()

    _patch_guards(monkeypatch)
    monkeypatch.setattr(service_wrapper, "run_once", _fake_run_once)
    with TestClient(service_wrapper.app) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "anila-agent", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["usage"] == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}


def test_http_stream_false_omits_usage_without_context_wrapper(monkeypatch):
    class _Result:
        final_output = "答案"
        # 沒有 context_wrapper —— 拿不到 SDK usage 時必須省略欄位；
        # 送全 0 會被 CSP 當權威值蓋掉本地估算（記帳歸零）。

    async def _fake_run_once(*a, **k):
        return _Result()

    _patch_guards(monkeypatch)
    monkeypatch.setattr(service_wrapper, "run_once", _fake_run_once)
    with TestClient(service_wrapper.app) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "anila-agent", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200
    assert "usage" not in resp.json()


def test_http_stream_include_usage_without_real_usage_omits_usage_chunk(monkeypatch):
    """SDK 拿不到 usage 時，即使 client 帶 include_usage 也不送 usage chunk。

    CSP 消費端對缺 usage 的串流會落回本地 token 估算；送全 0 chunk 反而
    會被當權威值、記帳歸零（Codex 審查發現）。
    """
    _patch_guards(monkeypatch)
    monkeypatch.setattr(
        service_wrapper,
        "run_streamed",
        lambda *a, **k: _FakeStreaming([_delta_event("嗨")], usage=None),
    )
    with TestClient(service_wrapper.app) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "anila-agent",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        )
    assert resp.status_code == 200
    blocks = [b for b in resp.text.split("\n\n") if b.strip()]
    assert blocks[-1].strip() == "data: [DONE]"
    chunks = _parse_sse_blocks(resp.text)
    assert all("usage" not in c for c in chunks)


# ---- 串流：usage 只在 client 帶 stream_options.include_usage 時才送 --------------


def _parse_sse_blocks(sse_text: str) -> list[dict]:
    return [
        json.loads(b[len("data:"):].strip())
        for b in sse_text.split("\n\n")
        if b.strip() and b.strip() != "data: [DONE]"
    ]


def test_http_stream_include_usage_emits_usage_only_chunk_before_done(monkeypatch):
    _patch_guards(monkeypatch)
    monkeypatch.setattr(
        service_wrapper, "run_streamed", lambda *a, **k: _FakeStreaming([_delta_event("嗨")])
    )
    with TestClient(service_wrapper.app) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "anila-agent",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        )
    assert resp.status_code == 200
    blocks = [b for b in resp.text.split("\n\n") if b.strip()]
    assert blocks[-1].strip() == "data: [DONE]"

    chunks = _parse_sse_blocks(resp.text)
    finish_chunks = [c for c in chunks if c["choices"] and c["choices"][0].get("finish_reason")]
    usage_chunks = [c for c in chunks if c.get("choices") == [] and "usage" in c]

    assert len(finish_chunks) == 1
    assert "usage" not in finish_chunks[0]  # usage 不再無條件掛在 finish chunk 上
    assert len(usage_chunks) == 1  # 獨立的 usage-only chunk（choices: []）
    assert usage_chunks[0]["usage"] == {
        "prompt_tokens": 3,
        "completion_tokens": 2,
        "total_tokens": 5,
    }

    # usage-only chunk 必須在 finish chunk 之後、[DONE] 之前。
    finish_idx = next(i for i, c in enumerate(chunks) if c is finish_chunks[0])
    usage_idx = next(i for i, c in enumerate(chunks) if c is usage_chunks[0])
    assert finish_idx < usage_idx == len(chunks) - 1


def test_http_stream_without_include_usage_omits_usage(monkeypatch):
    _patch_guards(monkeypatch)
    monkeypatch.setattr(
        service_wrapper, "run_streamed", lambda *a, **k: _FakeStreaming([_delta_event("嗨")])
    )
    with TestClient(service_wrapper.app) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "anila-agent",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
    assert resp.status_code == 200
    assert '"usage"' not in resp.text  # 沒帶 include_usage → 整段串流不出現 usage


# ---- OpenAI 多模態 array content：抽 text parts / 純圖片 422 --------------------


def test_http_stream_false_accepts_array_content_text_parts(monkeypatch):
    class _Result:
        final_output = "答案"

    captured: dict = {}

    async def _fake_run_once(assembled, user_prompt, *, hooks=None):
        captured["prompt"] = user_prompt
        return _Result()

    _patch_guards(monkeypatch)
    monkeypatch.setattr(service_wrapper, "run_once", _fake_run_once)
    with TestClient(service_wrapper.app) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "anila-agent",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "第一段"},
                            {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
                            {"type": "text", "text": "第二段"},
                        ],
                    }
                ],
            },
        )
    assert resp.status_code == 200
    assert captured["prompt"] == "第一段\n第二段"  # 只抽 text parts，非文字 part 被忽略


def test_http_image_only_content_returns_clear_error(monkeypatch):
    _patch_guards(monkeypatch)
    with TestClient(service_wrapper.app) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "anila-agent",
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "image_url", "image_url": {"url": "http://x/y.png"}}],
                    }
                ],
            },
        )
    assert resp.status_code == 422
    assert "影像" in resp.json()["detail"]
