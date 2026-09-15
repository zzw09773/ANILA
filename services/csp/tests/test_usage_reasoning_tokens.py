# -*- coding: utf-8 -*-
"""B 期：思考 token 入庫、anila_meta.usage、thinking_applied。"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.api import proxy as proxy_api
from app.models.token_usage import TokenUsage
from app.services import usage_writer
from app.services.proxy import service as proxy_impl
from app.services.proxy.sampling import ANILA_THINKING_TIER_KEY
from app.services.proxy.usage import _estimate_token_count
from tests.conftest import login, make_user
from tests.test_thinking_selector import (
    QWEN_LIKE,
    _CapturingUpstream,
    _open_deep_conversation,
    _open_router_llm,
    _wire_proxy_to_test_db,
)


class _PayloadResponse:
    status_code = 200
    headers = {"content-type": "application/json"}

    def __init__(self, payload: dict):
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        return None


class _StreamResponse:
    status_code = 200

    def __init__(self, lines: list[str]):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _ConfigurableUpstream:
    last_body: dict | None = None
    payload: dict | None = None
    stream_lines: list[str] | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).last_body = json
        return _PayloadResponse(type(self).payload or {})

    def stream(self, method, url, json=None, headers=None):
        type(self).last_body = json
        return _StreamResponse(list(type(self).stream_lines or []))


def _wire_usage_proxy(monkeypatch, db_engine, *, payload=None, stream_lines=None):
    monkeypatch.setattr(
        "app.database.SessionLocal",
        sessionmaker(bind=db_engine, expire_on_commit=False),
    )
    monkeypatch.setattr(
        usage_writer,
        "SessionLocal",
        sessionmaker(bind=db_engine, expire_on_commit=False),
    )
    monkeypatch.setattr(usage_writer, "_usage_queue", None)
    _ConfigurableUpstream.last_body = None
    _ConfigurableUpstream.payload = payload
    _ConfigurableUpstream.stream_lines = stream_lines
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    monkeypatch.setattr(
        proxy_impl.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _ConfigurableUpstream(*args, **kwargs),
    )
    monkeypatch.setattr(proxy_api, "_schedule_memory_write", lambda **kwargs: None)


def _flush_usage():
    queue = usage_writer.get_usage_queue()
    batch = []
    while not queue.empty():
        batch.append(queue.get_nowait())
    if batch:
        asyncio.run(usage_writer._flush_batch(batch))


def _open_plain_conversation(client, db, username: str, *, model_name: str, **fields):
    make_user(db, username=username)
    model = _open_router_llm(db, model_name, primary=True, **fields)
    token = login(client, username)
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post(
        "/api/conversations",
        headers=headers,
        json={"title": "t", "origin": "anila-ui"},
    )
    assert created.status_code == 201, created.text
    return model, headers, created.json()


def _chat(client, headers, *, model: str, conv_id, stream: bool = False, **extra):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": stream,
        **extra,
    }
    return client.post(
        "/v1/chat/completions",
        headers={
            **headers,
            "X-ANILA-Conversation-Id": str(conv_id),
        },
        json=body,
    )


def _sse_meta(text: str) -> dict:
    event = None
    for raw_block in text.split("\n\n"):
        event = None
        data = None
        for line in raw_block.splitlines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = line.split(":", 1)[1].strip()
        if event == "anila.meta" and data and data != "[DONE]":
            return json.loads(data)
    raise AssertionError("no anila.meta in stream")


def _usage_row(db: Session, user_id: int) -> TokenUsage:
    _flush_usage()
    db.expire_all()
    return db.query(TokenUsage).filter(TokenUsage.user_id == user_id).one()


def test_nonstream_reported_completion_tokens_details(client, db, db_engine, monkeypatch):
    _wire_usage_proxy(
        monkeypatch,
        db_engine,
        payload={
            "choices": [{"message": {"role": "assistant", "content": "answer"}}],
            "usage": {
                "prompt_tokens": 9,
                "completion_tokens": 4,
                "total_tokens": 13,
                "completion_tokens_details": {"reasoning_tokens": 1234},
            },
        },
    )
    model, headers, conv = _open_plain_conversation(
        client, db, "reason-ns-reported", model_name="glm-reason-ns"
    )
    resp = _chat(client, headers, model=model.name, conv_id=conv["id"], stream=False)
    assert resp.status_code == 200, resp.text
    meta = resp.json()["anila_meta"]
    assert meta["usage"]["reasoning_tokens"] == 1234
    assert meta["usage"]["reasoning_tokens_source"] == "reported"
    from app.models.user import User

    owner = db.query(User).filter(User.username == "reason-ns-reported").one()
    row = _usage_row(db, owner.id)
    assert row.reasoning_tokens == 1234
    assert row.token_source == "reported"


def test_stream_reported_top_level_reasoning_tokens(client, db, db_engine, monkeypatch):
    _wire_usage_proxy(
        monkeypatch,
        db_engine,
        stream_lines=[
            'data: {"choices":[{"index":0,"delta":{"content":"answer"},'
            '"finish_reason":"stop"}],"usage":{"prompt_tokens":11,'
            '"completion_tokens":7,"reasoning_tokens":1234}}',
            "",
            "data: [DONE]",
            "",
        ],
    )
    model, headers, conv = _open_plain_conversation(
        client, db, "reason-st-reported", model_name="glm-reason-st"
    )
    resp = _chat(client, headers, model=model.name, conv_id=conv["id"], stream=True)
    assert resp.status_code == 200, resp.text
    meta = _sse_meta(resp.text)
    assert meta["usage"]["reasoning_tokens"] == 1234
    assert meta["usage"]["reasoning_tokens_source"] == "reported"
    from app.models.user import User

    owner = db.query(User).filter(User.username == "reason-st-reported").one()
    row = _usage_row(db, owner.id)
    assert row.reasoning_tokens == 1234
    assert row.token_source == "reported"


def test_nonstream_estimates_reasoning_content(client, db, db_engine, monkeypatch):
    reasoning = "Let me reason about this answer in some detail."
    expected = _estimate_token_count("glm-reason-est", reasoning)
    _wire_usage_proxy(
        monkeypatch,
        db_engine,
        payload={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "answer",
                        "reasoning_content": reasoning,
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 2,
                "total_tokens": 5,
            },
        },
    )
    model, headers, conv = _open_plain_conversation(
        client, db, "reason-ns-est", model_name="glm-reason-est"
    )
    resp = _chat(client, headers, model=model.name, conv_id=conv["id"], stream=False)
    assert resp.status_code == 200, resp.text
    meta = resp.json()["anila_meta"]
    assert meta["usage"]["reasoning_tokens"] == expected
    assert meta["usage"]["reasoning_tokens_source"] == "estimated"
    from app.models.user import User

    owner = db.query(User).filter(User.username == "reason-ns-est").one()
    row = _usage_row(db, owner.id)
    assert row.reasoning_tokens == expected
    assert row.token_source == "reported"


def test_nonstream_missing_reasoning_is_none(client, db, db_engine, monkeypatch):
    _wire_usage_proxy(
        monkeypatch,
        db_engine,
        payload={
            "choices": [{"message": {"role": "assistant", "content": "answer"}}],
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 2,
                "total_tokens": 5,
            },
        },
    )
    model, headers, conv = _open_plain_conversation(
        client, db, "reason-ns-none", model_name="glm-reason-none"
    )
    resp = _chat(client, headers, model=model.name, conv_id=conv["id"], stream=False)
    assert resp.status_code == 200, resp.text
    meta = resp.json()["anila_meta"]
    assert meta["usage"]["reasoning_tokens"] is None
    assert meta["usage"]["reasoning_tokens_source"] is None
    from app.models.user import User

    owner = db.query(User).filter(User.username == "reason-ns-none").one()
    row = _usage_row(db, owner.id)
    assert row.reasoning_tokens is None


def test_thinking_applied_conversation_deep(client, db, db_engine, monkeypatch):
    _wire_proxy_to_test_db(monkeypatch, db_engine)
    model, headers, conv = _open_deep_conversation(
        client,
        db,
        "think-applied-deep",
        model_name="glm-applied-deep",
        thinking_levels_supported=QWEN_LIKE,
        thinking_effort=None,
    )
    resp = _chat(client, headers, model=model.name, conv_id=conv["id"], stream=False)
    assert resp.status_code == 200, resp.text
    applied = resp.json()["anila_meta"]["thinking_applied"]
    assert applied == {"tier": "deep", "level": "xhigh", "source": "conversation"}
    outbound = _CapturingUpstream.last_body
    assert outbound["reasoning_effort"] == "xhigh"
    assert ANILA_THINKING_TIER_KEY not in outbound


def test_thinking_applied_caller_reasoning_effort(client, db, db_engine, monkeypatch):
    _wire_proxy_to_test_db(monkeypatch, db_engine)
    model, headers, conv = _open_deep_conversation(
        client,
        db,
        "think-applied-caller",
        model_name="glm-applied-caller",
        thinking_levels_supported=QWEN_LIKE,
        thinking_effort=None,
    )
    resp = _chat(
        client,
        headers,
        model=model.name,
        conv_id=conv["id"],
        stream=False,
        reasoning_effort="low",
    )
    assert resp.status_code == 200, resp.text
    applied = resp.json()["anila_meta"]["thinking_applied"]
    assert applied["source"] == "caller"
    assert applied["level"] == "low"
    outbound = _CapturingUpstream.last_body
    assert outbound["reasoning_effort"] == "low"


def test_thinking_applied_model_default(client, db, db_engine, monkeypatch):
    _wire_proxy_to_test_db(monkeypatch, db_engine)
    model, headers, conv = _open_plain_conversation(
        client,
        db,
        "think-applied-model",
        model_name="glm-applied-model",
        thinking_levels_supported=QWEN_LIKE,
        thinking_effort=None,
    )
    resp = _chat(client, headers, model=model.name, conv_id=conv["id"], stream=False)
    assert resp.status_code == 200, resp.text
    applied = resp.json()["anila_meta"]["thinking_applied"]
    assert applied["source"] == "model"
    assert applied["tier"] == "default"


def test_describe_thinking_applied_does_not_change_apply_body():
    from app.services.proxy.sampling import (
        apply_model_sampling_overrides,
        describe_thinking_applied,
    )

    model = SimpleNamespace(
        name="qwen3",
        model_type="llm",
        protocol="openai_compatible",
        thinking_effort=None,
        thinking_levels_supported=QWEN_LIKE,
        thinking_user_selectable=True,
        temperature=None,
        top_p=None,
        presence_penalty=None,
        max_tokens=None,
    )
    body = {"messages": []}
    applied = describe_thinking_applied(body, model, thinking_tier="deep")
    out = apply_model_sampling_overrides(body, model, thinking_tier="deep")
    assert applied == {"tier": "deep", "level": "xhigh", "source": "conversation"}
    assert out["reasoning_effort"] == "xhigh"
    assert ANILA_THINKING_TIER_KEY not in out
    assert "messages" in out


def test_reasoning_tokens_source_null_only_when_tokens_missing():
    from app.services.proxy.usage import resolve_reasoning_tokens

    assert resolve_reasoning_tokens({"reasoning_tokens": 3}) == (3, "reported")
    assert resolve_reasoning_tokens(
        {"completion_tokens_details": {"reasoning_tokens": 8}}
    ) == (8, "reported")
    estimated_n, estimated_src = resolve_reasoning_tokens(
        {}, reasoning_text="abcd efgh ijkl", model_name=None
    )
    assert estimated_n is not None
    assert estimated_src == "estimated"
    assert resolve_reasoning_tokens({}, reasoning_text="") == (None, None)
    assert resolve_reasoning_tokens(None) == (None, None)


def _run_proxy_stream(monkeypatch, lines: list[str]) -> tuple[str, list[dict]]:
    from app.services import proxy_service
    from app.services.proxy.service import ProxyTuning

    recorded: list[dict] = []
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    monkeypatch.setattr(
        proxy_service.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _ConfigurableUpstream(*args, **kwargs),
    )
    _ConfigurableUpstream.stream_lines = lines
    _ConfigurableUpstream.last_body = None

    async def fake_enqueue(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(proxy_impl, "enqueue_usage", fake_enqueue)
    monkeypatch.setattr(proxy_impl, "enqueue_usage_task_linked", fake_enqueue)

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
            tuning=ProxyTuning.from_registry_defaults(),
        ):
            chunks.append(chunk)
        return chunks

    joined = "".join(asyncio.run(run()))
    return joined, recorded


def _all_sse_metas(text: str) -> list[dict]:
    frames: list[dict] = []
    for raw_block in text.split("\n\n"):
        event = None
        data = None
        for line in raw_block.splitlines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = line.split(":", 1)[1].strip()
        if event == "anila.meta" and data and data != "[DONE]":
            frames.append(json.loads(data))
    return frames


def test_stream_holds_named_meta_until_usage_merges_reasoning(monkeypatch):
    joined, recorded = _run_proxy_stream(
        monkeypatch,
        [
            'data: {"choices":[{"index":0,"delta":{"content":"answer"},'
            '"finish_reason":null}]}',
            "",
            "event: anila.meta",
            'data: {"kb_hits":[{"id":1,"title":"reg"}],"kb_state":"searched_hit"}',
            "",
            'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
            '"usage":{"prompt_tokens":11,"completion_tokens":7,'
            '"reasoning_tokens":1234}}',
            "",
            "data: [DONE]",
            "",
        ],
    )
    metas = _all_sse_metas(joined)
    assert len(metas) == 1
    assert metas[0]["kb_hits"] == [{"id": 1, "title": "reg"}]
    assert metas[0]["kb_state"] == "searched_hit"
    assert metas[0]["usage"]["reasoning_tokens"] == 1234
    assert metas[0]["usage"]["reasoning_tokens_source"] == "reported"
    assert joined.index("anila.meta") < joined.index("[DONE]")
    assert recorded[0]["reasoning_tokens"] == 1234


def test_stream_merges_usage_chunks_without_dropping_reasoning(monkeypatch):
    joined, recorded = _run_proxy_stream(
        monkeypatch,
        [
            'data: {"choices":[{"index":0,"delta":{"content":"Hi"},'
            '"finish_reason":null}],"usage":{"prompt_tokens":5,'
            '"completion_tokens":1,"reasoning_tokens":1234}}',
            "",
            'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
            '"usage":{"prompt_tokens":11,"completion_tokens":7}}',
            "",
            "data: [DONE]",
            "",
        ],
    )
    metas = _all_sse_metas(joined)
    assert metas[-1]["usage"]["reasoning_tokens"] == 1234
    assert metas[-1]["usage"]["reasoning_tokens_source"] == "reported"
    assert recorded[0]["prompt_tokens"] == 11
    assert recorded[0]["completion_tokens"] == 7
    assert recorded[0]["reasoning_tokens"] == 1234
