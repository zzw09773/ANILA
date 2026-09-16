# -*- coding: utf-8 -*-
"""B 期：思考 token 入庫、anila_meta.usage、thinking_applied。"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
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

    def __init__(self, lines: list[str], fail_exc: BaseException | None = None):
        self._lines = lines
        self._fail_exc = fail_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line
        if self._fail_exc is not None:
            raise self._fail_exc


class _ConfigurableUpstream:
    last_body: dict | None = None
    payload: dict | None = None
    stream_lines: list[str] | None = None
    stream_fail_exc: BaseException | None = None

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
        return _StreamResponse(
            list(type(self).stream_lines or []),
            fail_exc=type(self).stream_fail_exc,
        )


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
    _ConfigurableUpstream.stream_fail_exc = None
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
    assert applied["tier"] == "deep"
    assert applied["level"] == "xhigh"
    assert applied["source"] == "conversation"
    assert applied["enable_thinking"] is True
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
    assert applied["tier"] == "deep"
    assert applied["level"] == "xhigh"
    assert applied["source"] == "conversation"
    assert applied["enable_thinking"] is True
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


def _run_proxy_stream(
    monkeypatch,
    lines: list[str],
    *,
    fail_exc: BaseException | None = None,
) -> tuple[str, list[dict]]:
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
    _ConfigurableUpstream.stream_fail_exc = fail_exc
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
    assert metas[0]["usage_complete"] is True
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
    assert metas[-1]["usage_complete"] is True
    assert recorded[0]["prompt_tokens"] == 11
    assert recorded[0]["completion_tokens"] == 7
    assert recorded[0]["reasoning_tokens"] == 1234


def _named_sse_events(text: str) -> list[str]:
    names: list[str] = []
    for raw_block in text.split("\n\n"):
        event = None
        for line in raw_block.splitlines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
                break
        if event:
            names.append(event)
    return names


def test_stream_flushes_held_meta_before_timeout_error(monkeypatch):
    joined, recorded = _run_proxy_stream(
        monkeypatch,
        [
            'data: {"choices":[{"index":0,"delta":{"content":"partial"},'
            '"finish_reason":null}]}',
            "",
            "event: anila.meta",
            'data: {"kb_hits":[{"id":1,"title":"reg"}],"citations":[{"id":"c1"}]}',
            "",
        ],
        fail_exc=httpx.ReadTimeout("upstream stalled"),
    )
    metas = _all_sse_metas(joined)
    assert len(metas) == 1
    assert joined.count("event: anila.meta") == 1
    assert metas[0]["kb_hits"] == [{"id": 1, "title": "reg"}]
    assert metas[0]["citations"] == [{"id": "c1"}]
    assert metas[0]["usage_complete"] is False
    events = _named_sse_events(joined)
    assert events == ["anila.meta", "anila.error"]
    assert recorded == []


def test_stream_interrupt_flush_keeps_partial_reasoning_tokens(monkeypatch):
    joined, _recorded = _run_proxy_stream(
        monkeypatch,
        [
            "event: anila.meta",
            'data: {"kb_hits":[{"id":9}]}',
            "",
            'data: {"choices":[{"index":0,"delta":{},"finish_reason":null}],'
            '"usage":{"prompt_tokens":11,"completion_tokens":7,'
            '"reasoning_tokens":1234}}',
            "",
        ],
        fail_exc=httpx.ReadTimeout("cut after usage"),
    )
    metas = _all_sse_metas(joined)
    assert len(metas) == 1
    assert joined.count("event: anila.meta") == 1
    assert metas[0]["usage"]["reasoning_tokens"] == 1234
    assert metas[0]["usage"]["reasoning_tokens_source"] == "reported"
    assert metas[0]["usage_complete"] is False
    assert metas[0]["kb_hits"] == [{"id": 9}]
    assert _named_sse_events(joined) == ["anila.meta", "anila.error"]


def test_stream_normal_complete_emits_one_meta_usage_complete_true(monkeypatch):
    joined, _recorded = _run_proxy_stream(
        monkeypatch,
        [
            'data: {"choices":[{"index":0,"delta":{"content":"ok"},'
            '"finish_reason":"stop"}],"usage":{"prompt_tokens":2,'
            '"completion_tokens":1}}',
            "",
            "data: [DONE]",
            "",
        ],
    )
    metas = _all_sse_metas(joined)
    assert len(metas) == 1
    assert metas[0]["usage_complete"] is True
    assert "event: anila.error" not in joined


def test_stream_interrupt_without_named_meta_emits_only_error(monkeypatch):
    joined, recorded = _run_proxy_stream(
        monkeypatch,
        [
            'data: {"choices":[{"index":0,"delta":{"content":"Hel"},'
            '"finish_reason":null}]}',
            "",
        ],
        fail_exc=httpx.ReadTimeout("no meta yet"),
    )
    assert _all_sse_metas(joined) == []
    assert "event: anila.error" in joined
    assert "event: anila.meta" not in joined
    assert recorded == []


def test_stream_cancelled_after_named_meta_is_reraised(monkeypatch):
    with pytest.raises(asyncio.CancelledError):
        _run_proxy_stream(
            monkeypatch,
            [
                "event: anila.meta",
                'data: {"kb_hits":[{"id":1}]}',
                "",
            ],
            fail_exc=asyncio.CancelledError(),
        )


# ---------------------------------------------------------------------------
# 消費端收掉串流(client disconnect / task cancel)時的關閉路徑。
#
# 這裡的不變量是「關閉中不 yield」：暫存的 named meta 在關閉路徑上只能丟，
# 因為在 ``GeneratorExit`` 期間 yield 會變成 ``RuntimeError: async generator
# ignored GeneratorExit``，而在 cancel 期間 yield 會把取消吞掉。
# ---------------------------------------------------------------------------

#: named meta 先到（被暫存）、內容後到（會送給消費端）—— 關閉時手上一定有暫存 meta。
_HELD_META_THEN_CONTENT = [
    "event: anila.meta",
    'data: {"kb_hits":[{"id":1,"title":"reg"}]}',
    "",
    'data: {"choices":[{"index":0,"delta":{"content":"partial"},'
    '"finish_reason":null}]}',
    "",
    'data: {"choices":[{"index":0,"delta":{"content":"more"},'
    '"finish_reason":null}]}',
    "",
]


class _TeardownAwareStream:
    """上游串流，收尾會 await（因此可被取消），也可以指定收尾自己丟錯。

    真實 transport 的 ``__aexit__`` 兩者都會發生，而兩者都會把正在傳播的
    ``GeneratorExit`` 換成別的例外 —— 那正是第二版修補漏掉的那條路。
    """

    status_code = 200

    def __init__(self, lines: list[str], *, exit_exc: BaseException | None):
        self._lines = lines
        self._exit_exc = exit_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        if self._exit_exc is not None:
            # 在任何 await 之前丟：即使正在取消中，也一定換掉傳播中的例外。
            raise self._exit_exc
        await asyncio.sleep(0.02)
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _TeardownAwareUpstream:
    lines: list[str] = []
    exit_exc: BaseException | None = None
    park_forever: bool = False

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, json=None, headers=None):
        lines = list(type(self).lines)
        if type(self).park_forever:
            return _ParkedStream(lines, exit_exc=type(self).exit_exc)
        return _TeardownAwareStream(lines, exit_exc=type(self).exit_exc)


class _ParkedStream(_TeardownAwareStream):
    """送完既有行數就停在 await 上，讓測試能在「正在等上游」時取消。"""

    async def aiter_lines(self):
        for line in self._lines:
            yield line
        await asyncio.sleep(30)
        yield "data: [DONE]"
        yield ""


def _wire_teardown_upstream(
    monkeypatch,
    *,
    lines: list[str],
    exit_exc: BaseException | None = None,
    park_forever: bool = False,
):
    from app.services import proxy_service

    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    _TeardownAwareUpstream.lines = lines
    _TeardownAwareUpstream.exit_exc = exit_exc
    _TeardownAwareUpstream.park_forever = park_forever
    monkeypatch.setattr(
        proxy_service.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _TeardownAwareUpstream(*args, **kwargs),
    )

    async def fake_enqueue(**kwargs):
        return None

    monkeypatch.setattr(proxy_impl, "enqueue_usage", fake_enqueue)
    monkeypatch.setattr(proxy_impl, "enqueue_usage_task_linked", fake_enqueue)


def _spy_on_stream_impl(monkeypatch) -> list:
    """收集 ``proxy_stream`` 內部建立的 ``_proxy_stream_impl`` generator。

    內層的關閉在生產環境是由 event loop 的 asyncgen finalizer 代跑的，測試要
    能直接對它施壓才能把競態釘死。
    """
    created: list = []
    real_impl = proxy_impl._proxy_stream_impl

    def spy(**kwargs):
        agen = real_impl(**kwargs)
        created.append(agen)
        return agen

    monkeypatch.setattr(proxy_impl, "_proxy_stream_impl", spy)
    return created


def _open_proxy_stream(**overrides):
    from app.services import proxy_service
    from app.services.proxy.service import ProxyTuning

    kwargs = {
        "target_url": "http://mock-llm/v1/chat/completions",
        "api_key_id": 1,
        "user_id": 2,
        "department_id": None,
        "usage_model_id": 3,
        "request_body": {
            "model": "google/gemma4",
            "messages": [{"role": "user", "content": "Say hello"}],
            "stream": True,
        },
        "model_name": "google/gemma4",
        "tuning": ProxyTuning.from_registry_defaults(),
    }
    kwargs.update(overrides)
    return proxy_service.proxy_stream(**kwargs)


def _trap_loop_errors(errors: list[dict]) -> None:
    asyncio.get_running_loop().set_exception_handler(
        lambda _loop, context: errors.append(context)
    )


def _loop_error_text(errors: list[dict]) -> str:
    return " | ".join(
        f"{ctx.get('message')}: {ctx.get('exception')!r}" for ctx in errors
    )


def test_public_stream_aclose_after_named_meta_does_not_ignore_generator_exit(
    monkeypatch,
):
    _wire_teardown_upstream(monkeypatch, lines=_HELD_META_THEN_CONTENT)
    errors: list[dict] = []

    async def run():
        _trap_loop_errors(errors)
        agen = _open_proxy_stream()
        seen = [await agen.__anext__()]
        await agen.aclose()
        with pytest.raises(StopAsyncIteration):
            await agen.__anext__()
        # 讓 event loop 的 asyncgen finalizer 把內層 generator 收完。
        await asyncio.sleep(0.05)
        return seen

    seen = asyncio.run(run())
    assert "partial" in seen[0]
    # 暫存的 named meta 沒有收件人：關閉路徑不補送，也不准炸。
    assert _all_sse_metas("".join(seen)) == []
    assert errors == [], _loop_error_text(errors)


def test_stream_close_cancelled_mid_cleanup_does_not_ignore_generator_exit(
    monkeypatch,
):
    """關閉中的上游收尾被取消 —— ``GeneratorExit`` 會被 ``CancelledError`` 換掉。"""
    _wire_teardown_upstream(monkeypatch, lines=_HELD_META_THEN_CONTENT)
    created = _spy_on_stream_impl(monkeypatch)
    errors: list[dict] = []

    async def run():
        _trap_loop_errors(errors)
        agen = _open_proxy_stream()
        await agen.__anext__()
        inner = created[0]
        closing = asyncio.ensure_future(inner.aclose())
        await asyncio.sleep(0)
        closing.cancel()
        outcome = await asyncio.gather(closing, return_exceptions=True)
        await agen.aclose()
        await asyncio.sleep(0.05)
        return outcome[0]

    outcome = asyncio.run(run())
    assert isinstance(outcome, asyncio.CancelledError)
    assert not isinstance(outcome, RuntimeError)
    assert errors == [], _loop_error_text(errors)


def test_stream_close_with_failing_cleanup_does_not_ignore_generator_exit(
    monkeypatch,
):
    """關閉中的上游收尾自己丟錯 —— 換掉 ``GeneratorExit`` 的是普通 ``Exception``。"""
    _wire_teardown_upstream(
        monkeypatch,
        lines=_HELD_META_THEN_CONTENT,
        exit_exc=httpx.ReadError("teardown failed"),
    )
    created = _spy_on_stream_impl(monkeypatch)
    errors: list[dict] = []

    async def run():
        _trap_loop_errors(errors)
        agen = _open_proxy_stream()
        await agen.__anext__()
        inner = created[0]
        outcome = await asyncio.gather(inner.aclose(), return_exceptions=True)
        await agen.aclose()
        await asyncio.sleep(0.05)
        return outcome[0]

    outcome = asyncio.run(run())
    assert isinstance(outcome, httpx.ReadError)
    assert errors == [], _loop_error_text(errors)


def test_public_stream_task_cancel_after_named_meta_propagates_cancel(monkeypatch):
    _wire_teardown_upstream(
        monkeypatch,
        lines=_HELD_META_THEN_CONTENT,
        park_forever=True,
    )
    errors: list[dict] = []

    async def run():
        _trap_loop_errors(errors)
        agen = _open_proxy_stream()
        seen = [await agen.__anext__(), await agen.__anext__()]
        pending = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0)
        pending.cancel()
        outcome = await asyncio.gather(pending, return_exceptions=True)
        await asyncio.sleep(0.05)
        return seen, pending.cancelled(), outcome[0]

    seen, cancelled, outcome = asyncio.run(run())
    assert isinstance(outcome, asyncio.CancelledError)
    assert cancelled is True
    # 取消之後不得多出任何 event —— 尤其不准把暫存 meta 當成 ``__anext__`` 的結果。
    assert _all_sse_metas("".join(seen)) == []
    assert _named_sse_events("".join(seen)) == []
    assert errors == [], _loop_error_text(errors)


def test_public_stream_task_cancel_with_failing_cleanup_emits_nothing(monkeypatch):
    """取消 + 收尾丟錯：外層拿到的是被換掉的例外，同樣不准 yield。

    這條打的是公開 ``proxy_stream``：內層照規矩把替身例外往上丟之後，外層的
    ``except`` 只要 yield ``anila.error``，就等於把取消吞成一個正常事件。
    """
    _wire_teardown_upstream(
        monkeypatch,
        lines=_HELD_META_THEN_CONTENT,
        exit_exc=httpx.ReadError("teardown failed"),
        park_forever=True,
    )
    errors: list[dict] = []

    async def run():
        _trap_loop_errors(errors)
        agen = _open_proxy_stream()
        seen = [await agen.__anext__(), await agen.__anext__()]
        pending = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0)
        pending.cancel()
        outcome = await asyncio.gather(pending, return_exceptions=True)
        await asyncio.sleep(0.05)
        return seen, outcome[0]

    seen, outcome = asyncio.run(run())
    assert isinstance(outcome, BaseException), f"取消被吞成事件: {outcome!r}"
    assert not isinstance(outcome, RuntimeError)
    assert _named_sse_events("".join(seen)) == []
    assert errors == [], _loop_error_text(errors)


def test_agent_stream_interrupt_without_usage_has_no_short_reply(monkeypatch):
    from app.services import proxy_service
    from app.services.agent_reply_signal import (
        AGENT_REPLY_OBSERVATION_KEY,
        AGENT_REPLY_SHORT_FLAG,
    )

    monkeypatch.setattr(proxy_impl, "_guard_outbound", lambda *a, **k: None)
    monkeypatch.setattr(proxy_impl, "build_agent_headers", lambda **_k: {})
    monkeypatch.setattr(
        proxy_service.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _ConfigurableUpstream(*args, **kwargs),
    )
    _ConfigurableUpstream.stream_lines = [
        "event: anila.meta",
        'data: {"trace_id":"agent-trace","kb_hits":[{"id":3}]}',
        "",
    ]
    _ConfigurableUpstream.stream_fail_exc = httpx.ReadTimeout("agent stalled")

    async def run():
        chunks = []
        async for chunk in _open_proxy_stream(
            target_url="http://agent/v1/chat/completions",
            model_name="registered-agent",
            target_agent_id=7,
        ):
            chunks.append(chunk)
        return chunks

    joined = "".join(asyncio.run(run()))
    metas = _all_sse_metas(joined)
    assert len(metas) == 1
    assert metas[0]["usage_complete"] is False
    assert metas[0]["kb_hits"] == [{"id": 3}]
    observation = metas[0][AGENT_REPLY_OBSERVATION_KEY]
    # 量不到長度就不下短回覆判斷：前端只認 reported/estimated，會直接忽略。
    assert observation["usage_source"] == "unavailable"
    assert observation["usage_source"] != "estimated"
    assert AGENT_REPLY_SHORT_FLAG not in observation
    assert _named_sse_events(joined) == ["anila.meta", "anila.error"]
