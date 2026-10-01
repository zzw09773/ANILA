"""原始思考不出伺服器。

假上游把唯一標記寫進 reasoning、think 標籤、thought 開頭與 meta。
一般使用者、副手、單位管理員與他們的 API 金鑰，串流與非串流的回應位元組
都不得出現這個標記。擁有者、管理員、開發者收得到。摘要失敗不擋回答。
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.models.api_key import ApiKeyModelPermission
from app.models.department import Department
from app.models.message import Message
from app.models.unit_admin_assignment import UnitAdminAssignment
from app.models.user import UserModelPermission
from app.services import proxy_service
from app.services.proxy import service as proxy_impl
from app.services.reasoning_gate import gate_completion_payload, gate_sse_stream
from app.services.thinking_summary import summarize_reasoning_detached
from tests.conftest import login, make_agent, make_api_key, make_model, make_user

MARKER = "ANILA_RAW_MARKER_7f3c9e"
REASONING = f"{MARKER} " + ("the model thinks in english. " * 3)
SUMMARY = "正在整理題目。"
ANSWER = "這是正式回答。"
CJK_ANSWER = "這是一段足夠長的中文回答用來通過密度檢查讓閘門把英文分析留在伺服器"
THOUGHT = (
    f"thought:\n{MARKER} the model is analyzing the question in english only.\n"
    f"DISPATCH: gemma26\n{CJK_ANSWER}"
)


def _line(payload) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False)


def _stream_lines() -> list[str]:
    return [
        _line({"choices": [{"index": 0, "delta": {"reasoning_content": REASONING}}]}),
        "",
        _line({"choices": [{"index": 0, "delta": {"reasoning": REASONING}}]}),
        "",
        _line({"choices": [{"index": 0, "delta": {"content": "<thi"}}]}),
        "",
        _line({"choices": [{"index": 0, "delta": {"content": f"nk>{MARKER}</think>可見答案。"}}]}),
        "",
        "event: anila.reasoning\n" + _line({"delta": MARKER}),
        "",
        "data: reasoning_content " + MARKER,
        "",
        "event: anila.meta\n" + _line({"reasoning": MARKER, "trace": [{"label": "模型"}]}),
        "",
        _line({
            "choices": [{"index": 0, "delta": {"content": ANSWER}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8, "reasoning_tokens": 4},
        }),
        "",
        "data: [DONE]",
        "",
    ]


def _json_body(*, content: str) -> dict:
    return {
        "id": "chatcmpl-reveal",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content,
                "reasoning_content": REASONING,
                "reasoning": REASONING,
            },
            "finish_reason": "stop",
        }],
        "anila_meta": {"reasoning": MARKER, "trace": [{"label": "直接回答"}]},
        "usage": {
            "prompt_tokens": 3,
            "completion_tokens": 5,
            "total_tokens": 8,
            "reasoning_tokens": 4,
        },
    }


class _UpstreamResponse:
    def __init__(self, lines: list[str], payload: dict):
        self._lines = lines
        self.status_code = 200
        self.headers = {"content-type": "application/json"}
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return json.loads(self.text)

    async def aread(self):
        return b""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _Upstream:
    lines: list[str] = []
    payload: dict = {}
    urls: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method, url, json=None, headers=None):
        type(self).urls.append(url)
        return _UpstreamResponse(type(self).lines, type(self).payload)

    async def post(self, url, json=None, headers=None):
        type(self).urls.append(url)
        return _UpstreamResponse(type(self).lines, type(self).payload)


def _install(monkeypatch, *, summary):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    _Upstream.lines = _stream_lines()
    _Upstream.payload = _json_body(content=f"<think>{MARKER}</think>{ANSWER}")
    _Upstream.urls = []
    monkeypatch.setattr(
        proxy_impl.httpx, "AsyncClient", lambda *a, **k: _Upstream(*a, **k),
    )
    monkeypatch.setattr(
        proxy_service.httpx, "AsyncClient", lambda *a, **k: _Upstream(*a, **k),
    )

    async def _no_usage(**kwargs):
        return None

    monkeypatch.setattr(proxy_impl, "enqueue_usage", _no_usage)
    monkeypatch.setattr(proxy_impl, "enqueue_usage_task_linked", _no_usage)
    monkeypatch.setattr("app.api.proxy._schedule_memory_write", lambda **kwargs: None)
    monkeypatch.setattr(
        "app.services.thinking_summary.summarize_reasoning_detached", summary,
    )


def _grant(db, user, model, raw_key: str | None = None):
    db.add(UserModelPermission(user_id=user.id, model_id=model.id))
    key = None
    if raw_key is not None:
        key = make_api_key(db, user, raw_key=raw_key)
        db.add(ApiKeyModelPermission(api_key_id=key.id, model_id=model.id))
    db.commit()
    return key


def _auth_jwt(client, username: str) -> dict:
    return {"Authorization": f"Bearer {login(client, username)}"}


async def _chinese_summary(**kwargs):
    return SUMMARY


@pytest.fixture
def revealed_model(db, monkeypatch):
    _install(monkeypatch, summary=_chinese_summary)
    return make_model(db, name="reveal-llm")


def _chat(client, headers, model: str, *, stream: bool):
    return client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "model": model,
            "messages": [{"role": "user", "content": "題目"}],
            "stream": stream,
        },
    )


def _assert_hidden(resp):
    assert resp.status_code == 200, resp.text
    raw = resp.content
    assert MARKER.encode() not in raw
    assert resp.headers.get("x-anila-reveal-reasoning") == "0"


def _assert_visible(resp):
    assert resp.status_code == 200, resp.text
    assert MARKER.encode() in resp.content
    assert resp.headers.get("x-anila-reveal-reasoning") == "1"


@pytest.mark.parametrize("role,sees", [
    ("user", False),
    ("deputy", False),
    ("developer", True),
    ("admin", True),
    ("owner", True),
])
@pytest.mark.parametrize("stream", [False, True])
def test_jwt_caller_bytes_hide_or_show_raw_marker(client, db, revealed_model, role, sees, stream):
    user = make_user(db, username=f"reveal-{role}-{'s' if stream else 'j'}", role=role)
    _grant(db, user, revealed_model)
    resp = _chat(client, _auth_jwt(client, user.username), revealed_model.name, stream=stream)
    if sees:
        _assert_visible(resp)
        return
    _assert_hidden(resp)
    if stream:
        text = resp.content.decode()
        assert SUMMARY in text
        assert ANSWER in text
        assert "可見答案" in text
        assert "[DONE]" in text
        assert "reasoning_content" not in text
        return
    body = resp.json()
    message = body["choices"][0]["message"]
    assert "reasoning" not in message
    assert "reasoning_content" not in message
    assert message["anila_thinking_summary"] == SUMMARY
    assert MARKER not in message["content"]
    assert ANSWER in message["content"]
    meta = body["anila_meta"]
    assert "reasoning" not in meta
    assert meta["trace"][0]["label"] == "直接回答"
    assert meta["thinking_summaries"][-1]["text"] == SUMMARY
    assert body["usage"]["reasoning_tokens"] == 4


def test_thought_prefix_keeps_the_directive_and_drops_the_marker(client, db, revealed_model):
    _Upstream.lines = [
        _line({"choices": [{"index": 0, "delta": {"content": THOUGHT}, "finish_reason": "stop"}]}),
        "",
        "data: [DONE]",
        "",
    ]
    _Upstream.payload = _json_body(content=THOUGHT)
    user = make_user(db, username="reveal-thought-user")
    admin = make_user(db, username="reveal-thought-admin", role="admin")
    _grant(db, user, revealed_model)
    hidden = _chat(client, _auth_jwt(client, user.username), revealed_model.name, stream=True)
    _assert_hidden(hidden)
    text = hidden.content.decode()
    assert "DISPATCH: gemma26" in text
    assert CJK_ANSWER in text
    quiet = _chat(client, _auth_jwt(client, user.username), revealed_model.name, stream=False)
    _assert_hidden(quiet)
    content = quiet.json()["choices"][0]["message"]["content"]
    assert "DISPATCH: gemma26" in content
    assert CJK_ANSWER in content
    shown = _chat(client, _auth_jwt(client, admin.username), revealed_model.name, stream=False)
    _assert_visible(shown)
    assert MARKER in shown.json()["choices"][0]["message"]["content"]


def test_unit_admin_and_api_keys_follow_the_key_owner(client, db, revealed_model):
    dept = Department(name="reveal-unit")
    db.add(dept)
    db.commit()
    unit_user = make_user(db, username="reveal-unit-admin", role="user", department_id=dept.id)
    db.add(UnitAdminAssignment(user_id=unit_user.id, department_id=dept.id))
    plain = make_user(db, username="reveal-key-user", role="user")
    admin = make_user(db, username="reveal-key-admin", role="admin")
    _grant(db, unit_user, revealed_model, raw_key="sk-reveal-unit-admin")
    _grant(db, plain, revealed_model, raw_key="sk-reveal-plain-user")
    _grant(db, admin, revealed_model, raw_key="sk-reveal-admin-key")
    db.commit()

    for headers in (
        _auth_jwt(client, unit_user.username),
        {"Authorization": "Bearer sk-reveal-unit-admin"},
        {"Authorization": "Bearer sk-reveal-plain-user"},
    ):
        for stream in (False, True):
            _assert_hidden(_chat(client, headers, revealed_model.name, stream=stream))

    for headers in (
        _auth_jwt(client, admin.username),
        {"Authorization": "Bearer sk-reveal-admin-key"},
    ):
        for stream in (False, True):
            _assert_visible(_chat(client, headers, revealed_model.name, stream=stream))


def test_summary_failure_still_finishes_without_the_marker(client, db, monkeypatch):
    async def _boom(**kwargs):
        raise RuntimeError("summary model down")

    _install(monkeypatch, summary=_boom)
    model = make_model(db, name="reveal-fail-llm")
    user = make_user(db, username="reveal-fail-user")
    _grant(db, user, model)
    headers = _auth_jwt(client, user.username)
    for stream in (False, True):
        resp = _chat(client, headers, model.name, stream=stream)
        _assert_hidden(resp)
        assert ANSWER in resp.content.decode()
        if stream:
            assert "[DONE]" in resp.content.decode()


def test_summary_echo_of_the_marker_is_not_forwarded():
    async def _echo(**kwargs):
        return "整理" + MARKER + "題"

    async def _upstream():
        yield _line({"choices": [{"delta": {"reasoning_content": REASONING}}]}) + "\n\n"
        yield _line({"choices": [{"delta": {"content": ANSWER}, "finish_reason": "stop"}]}) + "\n\n"
        yield "data: [DONE]\n\n"

    async def _run():
        parts = []
        async for chunk in gate_sse_stream(
            _upstream(), reveal=False, user_id=1, department_id=None, summarizer=_echo,
        ):
            parts.append(chunk)
        return "".join(parts)

    body = asyncio.run(_run())
    assert MARKER not in body
    assert ANSWER in body
    assert "[DONE]" in body
    assert "anila_thinking_summary" not in body


def test_hung_summarizer_does_not_block_the_answer():
    async def _hang(**kwargs):
        await asyncio.Event().wait()

    async def _upstream():
        yield _line({"choices": [{"delta": {"reasoning_content": REASONING}}]}) + "\n\n"
        yield _line({"choices": [{"delta": {"content": ANSWER}}]}) + "\n\n"
        yield "data: [DONE]\n\n"

    async def _run():
        parts = []
        async for chunk in gate_sse_stream(
            _upstream(), reveal=False, user_id=1, department_id=None, summarizer=_hang,
        ):
            parts.append(chunk)
        return "".join(parts)

    body = asyncio.run(asyncio.wait_for(_run(), timeout=2))
    assert MARKER not in body
    assert ANSWER in body
    assert body.rstrip().endswith("data: [DONE]") or "[DONE]" in body


def test_detached_summary_closes_the_session_before_http(monkeypatch):
    order: list[str] = []

    class _Session:
        def commit(self):
            order.append("commit")

        def close(self):
            order.append("close")

    monkeypatch.setattr("app.database.SessionLocal", lambda: _Session())
    monkeypatch.setattr(
        "app.services.thinking_summary._summary_model",
        lambda db: object(),
    )
    monkeypatch.setattr(
        "app.services.proxy.service.resolve_proxy_tuning",
        lambda db: object(),
    )
    monkeypatch.setattr(
        "app.services.internal_llm._snapshot",
        lambda model: type("Snap", (), {"name": "gemma"})(),
    )

    async def _prepared(*args, **kwargs):
        order.append("http")
        return SUMMARY

    monkeypatch.setattr("app.services.internal_llm.complete_chat_prepared", _prepared)
    out = asyncio.run(summarize_reasoning_detached(added="原文" * 30, user_id=1))
    assert out == SUMMARY
    assert order.index("close") < order.index("http")
    assert "commit" in order


@pytest.mark.parametrize("role,sees", [("user", False), ("developer", True)])
@pytest.mark.parametrize("stream", [False, True])
def test_dispatch_jwt_hides_raw_reasoning_unless_the_end_user_may_see_it(
    client, db, revealed_model, role, sees, stream,
):
    """派工 JWT 打核准的底層模型。頭與正文要一致：非特權提問者看不到原文。"""
    from app.services.proxy.dispatch_token import issue_dispatch_token

    asker = make_user(db, username=f"dispatch-reveal-{role}", role=role)
    owner = make_user(db, username=f"dispatch-reveal-owner-{role}", role="developer")
    agent = make_agent(
        db,
        owner,
        name=f"dispatch-reveal-{role}-{int(stream)}",
        approval_status="approved",
    )
    agent.base_model_id = revealed_model.id
    db.commit()
    token = issue_dispatch_token(user_id=asker.id, department=None, agent_id=agent.id)
    resp = _chat(
        client,
        {"Authorization": f"Bearer {token}"},
        revealed_model.name,
        stream=stream,
    )
    if sees:
        _assert_visible(resp)
        return
    _assert_hidden(resp)
    raw = resp.content.decode()
    assert ANSWER in raw
    assert "reasoning_content" not in raw
    if not stream:
        body = resp.json()
        assert body["usage"]["reasoning_tokens"] == 4
        assert "reasoning" not in body["anila_meta"]


def test_redact_message_metadata_walks_nested_reasoning_keys():
    """巢狀 reasoning／reasoning_content 也要拿掉，而且不改到呼叫端的 dict。"""
    from app.services.reasoning_gate import redact_message_metadata

    metadata = {
        "reasoning_content": MARKER,
        "anila_meta": {
            "reasoning": MARKER,
            "trace": [{"label": "直接回答", "reasoning_content": MARKER}],
        },
        "steps": [{"reasoning": MARKER}, {"note": "keep"}],
        "thinking_summaries": [{"text": SUMMARY}],
    }
    cleaned = redact_message_metadata(metadata, reveal=False)
    blob = json.dumps(cleaned, ensure_ascii=False)
    assert MARKER not in blob
    assert "reasoning_content" not in cleaned
    assert "reasoning" not in cleaned["anila_meta"]
    assert "reasoning_content" not in cleaned["anila_meta"]["trace"][0]
    assert cleaned["anila_meta"]["trace"][0]["label"] == "直接回答"
    assert cleaned["steps"] == [{}, {"note": "keep"}]
    assert cleaned["thinking_summaries"][0]["text"] == SUMMARY
    assert metadata["reasoning_content"] == MARKER
    assert metadata["anila_meta"]["reasoning"] == MARKER
    assert redact_message_metadata(metadata, reveal=True) is metadata


def test_reload_drops_nested_reasoning_keys_for_non_privileged(client, db):
    user = make_user(db, username="reveal-nested-user")
    admin = make_user(db, username="reveal-nested-admin", role="admin")
    user_headers = _auth_jwt(client, user.username)
    admin_headers = _auth_jwt(client, admin.username)
    metadata = {
        "reasoning_content": MARKER,
        "anila_meta": {"reasoning": MARKER, "trace": [{"label": "直接回答"}]},
        "steps": [{"reasoning_content": MARKER}, {"note": "keep"}],
        "thinking_summaries": [{"text": SUMMARY}],
    }

    def _open(headers):
        created = client.post(
            "/api/conversations",
            headers=headers,
            json={"title": "巢狀原文", "origin": "anila-ui"},
        )
        assert created.status_code == 201, created.text
        return created.json()["id"]

    def _append(headers, conv_id):
        resp = client.post(
            f"/api/conversations/{conv_id}/messages",
            headers=headers,
            json={"role": "assistant", "content": ANSWER, "metadata": metadata},
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    user_conv = _open(user_headers)
    written = _append(user_headers, user_conv)
    blob = json.dumps(written.get("metadata") or {}, ensure_ascii=False)
    assert MARKER not in blob
    assert written["metadata"]["anila_meta"]["trace"][0]["label"] == "直接回答"
    assert written["metadata"]["steps"][1]["note"] == "keep"
    assert written["metadata"]["thinking_summaries"][0]["text"] == SUMMARY
    db.expire_all()
    stored = db.query(Message).filter(Message.id == written["id"]).one()
    assert MARKER not in json.dumps(stored.metadata_ or {}, ensure_ascii=False)
    reloaded = client.get(f"/api/conversations/{user_conv}", headers=user_headers)
    assert reloaded.status_code == 200, reloaded.text
    assert MARKER.encode() not in reloaded.content

    admin_conv = _open(admin_headers)
    admin_row = _append(admin_headers, admin_conv)
    assert admin_row["metadata"]["anila_meta"]["reasoning"] == MARKER
    assert admin_row["metadata"]["reasoning_content"] == MARKER


def test_reload_and_write_drop_stored_reasoning_for_non_privileged(client, db):
    user = make_user(db, username="reveal-reload-user")
    admin = make_user(db, username="reveal-reload-admin", role="admin")
    developer = make_user(db, username="reveal-reload-dev", role="developer")
    user_headers = _auth_jwt(client, user.username)
    admin_headers = _auth_jwt(client, admin.username)
    dev_headers = _auth_jwt(client, developer.username)

    def _open(headers):
        created = client.post(
            "/api/conversations",
            headers=headers,
            json={"title": "舊對話", "origin": "anila-ui"},
        )
        assert created.status_code == 201, created.text
        return created.json()["id"]

    def _append(headers, conv_id, metadata):
        resp = client.post(
            f"/api/conversations/{conv_id}/messages",
            headers=headers,
            json={
                "role": "assistant",
                "content": ANSWER,
                "metadata": metadata,
            },
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    user_conv = _open(user_headers)
    written = _append(user_headers, user_conv, {
        "reasoning": MARKER,
        "thinking_summaries": [{"text": SUMMARY}],
    })
    assert "reasoning" not in (written.get("metadata") or {})
    assert written["metadata"]["thinking_summaries"][0]["text"] == SUMMARY
    db.expire_all()
    stored = db.query(Message).filter(Message.id == written["id"]).one()
    assert "reasoning" not in (stored.metadata_ or {})

    stored.metadata_ = {"reasoning": MARKER, "thinking_summaries": [{"text": SUMMARY}]}
    db.commit()
    reloaded = client.get(f"/api/conversations/{user_conv}", headers=user_headers)
    assert reloaded.status_code == 200, reloaded.text
    blob = reloaded.content
    assert MARKER.encode() not in blob
    messages = reloaded.json()["messages"]
    assert messages[-1]["metadata"]["thinking_summaries"][0]["text"] == SUMMARY
    assert "reasoning" not in messages[-1]["metadata"]

    admin_conv = _open(admin_headers)
    admin_row = _append(admin_headers, admin_conv, {"reasoning": MARKER})
    assert admin_row["metadata"]["reasoning"] == MARKER
    admin_get = client.get(f"/api/conversations/{admin_conv}", headers=admin_headers)
    assert MARKER.encode() in admin_get.content

    dev_conv = _open(dev_headers)
    dev_row = _append(dev_headers, dev_conv, {"reasoning": MARKER})
    assert dev_row["metadata"]["reasoning"] == MARKER


INLINE = f"<think>{MARKER}</think>DISPATCH: gemma26\n這是正式回答。"


def _collect_gated(chunks: list[str], *, reveal: bool, summarizer=None) -> str:
    async def _upstream():
        for chunk in chunks:
            yield chunk

    async def _run():
        parts = []
        async for piece in gate_sse_stream(
            _upstream(),
            reveal=reveal,
            user_id=1,
            department_id=None,
            summarizer=summarizer,
        ):
            parts.append(piece)
        return "".join(parts)

    return asyncio.run(_run())


def test_error_frame_survives_and_drops_upstream_detail_for_non_privileged():
    detail = f"upstream reasoning {MARKER}"
    raw = "event: error\ndata: {\"status\": 502, \"detail\": " + repr(detail) + "}\n\n"
    body = _collect_gated([
        raw,
        _line({"choices": [{"delta": {"content": ANSWER}}]}) + "\n\n",
        "data: [DONE]\n\n",
    ], reveal=False)
    assert "event: error" in body
    assert "上游暫時無法回應" in body
    assert "502" in body
    assert MARKER not in body
    assert ANSWER in body
    assert body.index("event: error") < body.index("[DONE]")

    visible = _collect_gated([raw, "data: [DONE]\n\n"], reveal=True)
    assert MARKER in visible
    assert "event: error" in visible


def test_non_json_data_is_dropped_for_non_privileged():
    body = _collect_gated([
        f"data: 思考 {MARKER}\n\n",
        _line({"choices": [{"delta": {"content": ANSWER}}]}) + "\n\n",
        "data: [DONE]\n\n",
    ], reveal=False)
    assert MARKER not in body
    assert ANSWER in body
    assert "[DONE]" in body


def test_tool_call_arguments_do_not_carry_the_marker():
    args = json.dumps({"q": f"<think>{MARKER}</think>查詢"}, ensure_ascii=False)
    body = _collect_gated([
        _line({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"name": "search", "arguments": "<thi"}},
        ]}}]}) + "\n\n",
        _line({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": f"nk>{MARKER}</think>查詢"}},
        ]}}]}) + "\n\n",
        _line({"choices": [{"delta": {"content": ANSWER}}]}) + "\n\n",
        "data: [DONE]\n\n",
    ], reveal=False)
    assert MARKER not in body
    assert "查詢" in body
    assert ANSWER in body
    assert args.split(MARKER)[-1].strip(">") not in body or "查詢" in body


def test_non_stream_summary_does_not_hold_the_answer():
    async def _slow(**kwargs):
        await asyncio.sleep(2)
        return SUMMARY

    payload = _json_body(content=f"<think>{MARKER}</think>{ANSWER}")
    payload["choices"][0]["message"]["reasoning_content"] = MARKER + (" 想 " * 20)

    async def _run():
        return await gate_completion_payload(
            payload,
            reveal=False,
            user_id=1,
            department_id=None,
            summarizer=_slow,
        )

    started = time.perf_counter()
    out = asyncio.run(asyncio.wait_for(_run(), timeout=0.5))
    assert time.perf_counter() - started < 0.45
    blob = json.dumps(out, ensure_ascii=False)
    assert MARKER not in blob
    assert ANSWER in blob
    assert "anila_thinking_summary" not in blob


def test_stream_finish_keeps_a_summary_that_lands_inside_the_deadline():
    async def _quick(**kwargs):
        await asyncio.sleep(0.05)
        return SUMMARY

    reasoning = MARKER + ("思" * 20)
    body = _collect_gated([
        _line({"choices": [{"delta": {"reasoning_content": reasoning}}]}) + "\n\n",
        _line({"choices": [{"delta": {"content": ANSWER}}]}) + "\n\n",
        "data: [DONE]\n\n",
    ], reveal=False, summarizer=_quick)
    assert MARKER not in body
    assert SUMMARY in body
    assert body.index(SUMMARY) < body.index("[DONE]")


def test_stream_finish_drops_a_summary_that_misses_the_deadline():
    async def _slow(**kwargs):
        await asyncio.sleep(1.5)
        return SUMMARY

    reasoning = MARKER + ("思" * 20)
    started = time.perf_counter()
    body = _collect_gated([
        _line({"choices": [{"delta": {"reasoning_content": reasoning}}]}) + "\n\n",
        _line({"choices": [{"delta": {"content": ANSWER}}]}) + "\n\n",
        "data: [DONE]\n\n",
    ], reveal=False, summarizer=_slow)
    assert time.perf_counter() - started < 1.2
    assert SUMMARY not in body
    assert MARKER not in body
    assert "[DONE]" in body
    assert ANSWER in body


def test_v2_stream_hides_the_marker_and_sends_the_reveal_header(client, db, revealed_model):
    revealed_model.api_version = "v2"
    db.commit()
    user = make_user(db, username="reveal-v2-user")
    admin = make_user(db, username="reveal-v2-admin", role="admin")
    _grant(db, user, revealed_model)
    _grant(db, admin, revealed_model)
    _Upstream.urls = []
    hidden = _chat(client, _auth_jwt(client, user.username), revealed_model.name, stream=True)
    _assert_hidden(hidden)
    assert any("/v2/chat/completions" in url for url in _Upstream.urls)
    shown = _chat(client, _auth_jwt(client, admin.username), revealed_model.name, stream=True)
    _assert_visible(shown)
    assert any("/v2/chat/completions" in url for url in _Upstream.urls)


def test_credential_error_stream_sends_the_reveal_header(client, db, monkeypatch):
    from fastapi import HTTPException

    from app.services.proxy.headers import MODEL_CREDENTIAL_UNREADABLE

    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    user = make_user(db, username="reveal-cred-user")
    model = make_model(db, name="reveal-cred-model")
    _grant(db, user, model)

    def _boom(*_args, **_kwargs):
        raise HTTPException(status_code=503, detail=MODEL_CREDENTIAL_UNREADABLE)

    monkeypatch.setattr("app.api.proxy._prepare_platform_router_forward", _boom)
    resp = _chat(client, _auth_jwt(client, user.username), model.name, stream=True)
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("x-anila-reveal-reasoning") == "0"
    assert MARKER.encode() not in resp.content
    assert "event:" in resp.text


def test_agent_upstream_error_sends_the_reveal_header(client, db, monkeypatch):
    import httpx

    from app.models.agent import UserAgentPermission

    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "agent")
    user = make_user(db, username="reveal-agent-err")
    owner = make_user(db, username="reveal-agent-owner", role="developer")
    agent = make_agent(db, owner, name="reveal-agent-err", approval_status="approved")
    agent.endpoint_url = "http://agent:9100"
    db.add(UserAgentPermission(user_id=user.id, agent_id=agent.id))
    db.commit()

    class _Boom:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            request = httpx.Request("POST", "http://agent:9100/v1/chat/completions")
            response = httpx.Response(500, text=f"reasoning {MARKER}", request=request)
            raise httpx.HTTPStatusError("boom", request=request, response=response)

    monkeypatch.setattr(httpx, "AsyncClient", _Boom)
    resp = client.post(
        "/v1/chat/completions",
        headers=_auth_jwt(client, user.username),
        json={
            "model": agent.name,
            "messages": [{"role": "user", "content": "題目"}],
            "stream": False,
        },
    )
    assert resp.status_code == 500, resp.text
    assert resp.headers.get("x-anila-reveal-reasoning") == "0"
    assert MARKER.encode() not in resp.content


def test_read_and_write_strip_inline_thinking_for_non_privileged(client, db):
    user = make_user(db, username="reveal-inline-user")
    admin = make_user(db, username="reveal-inline-admin", role="admin")
    deputy = make_user(db, username="reveal-inline-deputy", role="deputy")
    owner = make_user(db, username="reveal-inline-owner", role="owner")
    user_headers = _auth_jwt(client, user.username)
    admin_headers = _auth_jwt(client, admin.username)
    deputy_headers = _auth_jwt(client, deputy.username)
    owner_headers = _auth_jwt(client, owner.username)

    created = client.post(
        "/api/conversations",
        headers=user_headers,
        json={"title": "行內思考", "origin": "anila-ui"},
    )
    assert created.status_code == 201, created.text
    conv_id = created.json()["id"]
    written = client.post(
        f"/api/conversations/{conv_id}/messages",
        headers=user_headers,
        json={"role": "assistant", "content": INLINE},
    )
    assert written.status_code == 201, written.text
    assert MARKER not in written.json()["content"]
    assert "DISPATCH: gemma26" in written.json()["content"]
    assert "這是正式回答。" in written.json()["content"]
    db.expire_all()
    stored = db.query(Message).filter(Message.id == written.json()["id"]).one()
    assert MARKER not in (stored.content or "")

    stored.content = INLINE
    db.commit()
    reloaded = client.get(f"/api/conversations/{conv_id}", headers=user_headers)
    assert MARKER.encode() not in reloaded.content
    assert "這是正式回答。" in reloaded.text
    assert "DISPATCH: gemma26" in reloaded.text

    admin_conv = client.post(
        "/api/conversations",
        headers=admin_headers,
        json={"title": "管理員原文", "origin": "anila-ui"},
    ).json()["id"]
    admin_row = client.post(
        f"/api/conversations/{admin_conv}/messages",
        headers=admin_headers,
        json={"role": "assistant", "content": INLINE},
    )
    assert admin_row.status_code == 201, admin_row.text
    assert MARKER in admin_row.json()["content"]

    owner_conv = client.post(
        "/api/conversations",
        headers=owner_headers,
        json={"title": "分享原文", "origin": "anila-ui"},
    ).json()["id"]
    owner_row = client.post(
        f"/api/conversations/{owner_conv}/messages",
        headers=owner_headers,
        json={"role": "assistant", "content": INLINE},
    )
    assert MARKER in owner_row.json()["content"]
    shared = client.post(
        f"/api/conversations/{owner_conv}/shares",
        headers=owner_headers,
        json={"target_username": deputy.username},
    )
    assert shared.status_code == 201, shared.text
    peeked = client.get(f"/api/conversations/{owner_conv}", headers=deputy_headers)
    assert peeked.status_code == 200, peeked.text
    assert MARKER.encode() not in peeked.content
    assert "這是正式回答。" in peeked.text
    owner_get = client.get(f"/api/conversations/{owner_conv}", headers=owner_headers)
    assert MARKER.encode() in owner_get.content

    needle = client.get(
        f"/api/conversations/search?q={MARKER}",
        headers=user_headers,
    )
    assert needle.status_code == 200, needle.text
    assert MARKER.encode() not in needle.content
    for hit in needle.json():
        assert not hit.get("snippet") or MARKER not in hit["snippet"]
    found = client.get(
        "/api/conversations/search?q=這是正式回答",
        headers=user_headers,
    )
    assert found.status_code == 200, found.text
    snippets = [hit.get("snippet") or "" for hit in found.json()]
    assert any("這是正式回答" in snippet for snippet in snippets)
    assert all(MARKER not in snippet for snippet in snippets)

    adopted = client.post(
        "/api/conversations/adopt",
        headers=user_headers,
        json={
            "title": "採用比較",
            "origin": "anila-ui",
            "user_content": f"<think>{MARKER}</think>使用者問題",
            "assistant_content": INLINE,
        },
    )
    assert adopted.status_code == 201, adopted.text
    assert MARKER.encode() not in adopted.content
    texts = [item["content"] for item in adopted.json()["messages"]]
    assert any("使用者問題" in text for text in texts)
    assert any("這是正式回答。" in text for text in texts)
    assert any("DISPATCH: gemma26" in text for text in texts)
