"""追問建議停用契約：所有生成與快取回放回空，權限與狀態檢查仍有效。

驗證未核定文字不出站、設定不能啟用、舊快取不回放、沒有模型請求，
以及 ownership、CSRF 與訊息狀態的存取限制。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.models.conversation import Conversation, ConversationShare
from app.models.message import Message
from app.models.model_role import ModelRole
from app.models.platform_setting import get_setting, set_setting
from app.services.follow_ups_service import (
    FOLLOW_UP_PROMPT,
    followups_concurrency_limit,
    followups_timeout_seconds,
    parse_follow_ups,
    release,
    reset_concurrency,
    try_acquire,
)
from tests.conftest import login, make_model, make_user

_LINES = "\n".join((
    "1. 特休可以遞延到明年嗎",
    "2、病假需要附證明嗎",
    "（3）「剩餘天數在哪裡查」",
    "",
    "特休可以遞延到明年嗎",
))
_PARSED = ["特休可以遞延到明年嗎", "病假需要附證明嗎", "剩餘天數在哪裡查"]
_ANSWER = "答" * 40


@pytest.fixture(autouse=True)
def _isolate_followups(monkeypatch):
    for name in (
        "FOLLOW_UPS_ENABLED",
        "FOLLOW_UPS_ALLOW_CLASSIFIED",
        "FOLLOW_UPS_MAX",
        "FOLLOWUPS_TIMEOUT_S",
        "FOLLOWUPS_MAX_CONCURRENCY",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_concurrency()
    yield
    reset_concurrency()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _post(client, token: str, conv_id: int, message_id: int, **headers):
    merged = _auth(token)
    merged.update(headers)
    return client.post(
        f"/api/conversations/{conv_id}/messages/{message_id}/follow-ups",
        headers=merged,
    )


def _summary(db, name: str, *, internal: bool = False):
    row = make_model(db, name=name)
    row.is_internal = internal
    link = db.get(ModelRole, "summary")
    if link is None:
        db.add(ModelRole(role="summary", model_id=row.id))
    else:
        link.model_id = row.id
    db.commit()
    return row


def _primary(db, name: str, *, internal: bool = False):
    row = make_model(db, name=name)
    row.is_internal = internal
    row.is_router_primary = True
    db.commit()
    return row


def _pair(
    db,
    user,
    *,
    answer: str = _ANSWER,
    question: str = "年假還能請幾天",
    role: str = "assistant",
    state: str | None = "complete",
    extra: dict | None = None,
    classified: bool = False,
    parent: Message | None = None,
):
    conv = Conversation(
        user_id=user.id,
        title="追問",
        origin="anila-ui",
        classified=classified,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    user_msg = Message(
        conversation_id=conv.id,
        role="user",
        content=question,
        classification_level="無機密",
    )
    db.add(user_msg)
    db.flush()
    meta: dict | None
    if state is None and not extra:
        meta = None
    else:
        meta = {}
        if state is not None:
            meta["anila_stream"] = {"state": state}
        if extra:
            meta.update(extra)
    asst = Message(
        conversation_id=conv.id,
        parent_id=user_msg.id if parent is None else parent.id,
        role=role,
        content=answer,
        metadata_=meta,
        classification_level="無機密",
    )
    db.add(asst)
    db.flush()
    conv.active_leaf_message_id = asst.id
    db.commit()
    db.refresh(asst)
    return conv, user_msg, asst


def _install(monkeypatch, content):
    captured: dict = {"calls": 0}

    async def fake(db, model, payload, **kwargs):
        captured["calls"] += 1
        captured["model"] = getattr(model, "name", None)
        captured["payload"] = payload
        captured["kwargs"] = kwargs
        if isinstance(content, Exception):
            raise content
        if asyncio.iscoroutinefunction(content):
            return await content()
        return content

    monkeypatch.setattr("app.services.internal_llm.complete_chat", fake)
    return captured


def _ready(client, db, username: str, content=_LINES, **pair_kwargs):
    user = make_user(db, username=username)
    token = login(client, username)
    _summary(db, f"{username}-summary")
    captured = _install(monkeypatch=pair_kwargs.pop("monkeypatch"), content=content)
    conv, user_msg, asst = _pair(db, user, **pair_kwargs)
    return user, token, captured, conv, user_msg, asst


def test_prompt_text_matches_the_design():
    assert FOLLOW_UP_PROMPT == (
        "以繁體中文（台灣用語）列出使用者最可能接著問的 3 個問題，每行一個，每個 30 字內，"
        "用使用者的口吻，不要重複已回答的內容，不要編造文件名稱；如果不適合追問就輸出 NONE。"
    )


def test_timeout_and_concurrency_env_fallbacks(monkeypatch):
    assert followups_timeout_seconds() == 6
    assert followups_concurrency_limit() == 4
    monkeypatch.setenv("FOLLOWUPS_TIMEOUT_S", "nope")
    monkeypatch.setenv("FOLLOWUPS_MAX_CONCURRENCY", "-1")
    assert followups_timeout_seconds() == 6
    assert followups_concurrency_limit() == 4
    monkeypatch.setenv("FOLLOWUPS_TIMEOUT_S", "0.2")
    monkeypatch.setenv("FOLLOWUPS_MAX_CONCURRENCY", "0")
    assert followups_timeout_seconds() == 0.2
    assert followups_concurrency_limit() == 0


def test_follow_up_settings_default_unavailable_and_retained_limits(db):
    assert get_setting(db, "follow_ups.enabled") is False
    assert get_setting(db, "follow_ups.allow_classified") is False
    assert get_setting(db, "follow_ups.max") == 3


def test_missing_user_message_does_not_call_model(client, db, monkeypatch):
    user = make_user(db, username="follow-nouser")
    token = login(client, "follow-nouser")
    _summary(db, "follow-nouser-summary")
    captured = _install(monkeypatch, _LINES)
    conv = Conversation(user_id=user.id, title="追問", origin="anila-ui")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    asst = Message(
        conversation_id=conv.id, role="assistant", content=_ANSWER,
        metadata_={"anila_stream": {"state": "complete"}}, classification_level="無機密",
    )
    db.add(asst)
    db.commit()
    resp = _post(client, token, conv.id, asst.id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["suggestions"] == []
    assert captured["calls"] == 0


def test_other_owner_admin_and_share_are_404(client, db, monkeypatch):
    owner = make_user(db, username="follow-owner")
    other = make_user(db, username="follow-other")
    admin = make_user(db, username="follow-admin", role="admin")
    shared = make_user(db, username="follow-shared")
    _summary(db, "follow-owner-summary")
    captured = _install(monkeypatch, _LINES)
    conv, _, asst = _pair(db, owner)
    db.add(ConversationShare(
        conversation_id=conv.id, target_user_id=shared.id, created_by=owner.id,
    ))
    db.commit()
    for username in (other.username, admin.username, shared.username):
        token = login(client, username)
        resp = _post(client, token, conv.id, asst.id)
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "對話不存在"
    assert captured["calls"] == 0
    missing = _post(client, login(client, "follow-owner"), 9_999_999, asst.id)
    assert missing.status_code == 404
    assert missing.json()["detail"] == "對話不存在"


def test_message_in_another_conversation_is_404(client, db, monkeypatch):
    user = make_user(db, username="follow-cross")
    token = login(client, "follow-cross")
    _summary(db, "follow-cross-summary")
    captured = _install(monkeypatch, _LINES)
    conv, _, _ = _pair(db, user, question="第一個對話")
    _, _, other = _pair(db, user, question="第二個對話")
    resp = _post(client, token, conv.id, other.id)
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "訊息不存在"
    assert captured["calls"] == 0


@pytest.mark.parametrize("role", ["system", "tool"])
def test_system_and_tool_messages_are_422(client, db, monkeypatch, role):
    user = make_user(db, username=f"follow-{role}")
    token = login(client, f"follow-{role}")
    _summary(db, f"follow-{role}-summary")
    captured = _install(monkeypatch, _LINES)
    conv, _, asst = _pair(db, user, role=role)
    resp = _post(client, token, conv.id, asst.id)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "只能為已完成的助理訊息產生建議"
    assert captured["calls"] == 0


def test_user_message_is_422(client, db, monkeypatch):
    _, token, captured, conv, user_msg, _ = _ready(
        client, db, "follow-user-msg", monkeypatch=monkeypatch,
    )
    resp = _post(client, token, conv.id, user_msg.id)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "只能為已完成的助理訊息產生建議"
    assert captured["calls"] == 0


@pytest.mark.parametrize("state", ["reserved", "streaming", "weird"])
def test_unfinished_or_unknown_state_is_422(client, db, monkeypatch, state):
    _, token, captured, conv, _, asst = _ready(
        client, db, f"follow-open-{state}", monkeypatch=monkeypatch, state=state,
    )
    resp = _post(client, token, conv.id, asst.id)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "只能為已完成的助理訊息產生建議"
    assert captured["calls"] == 0


@pytest.mark.parametrize("state", ["stopped", "failed", "interrupted", "unanswered"])
def test_incomplete_answer_is_empty_without_model(client, db, monkeypatch, state):
    _, token, captured, conv, _, asst = _ready(
        client, db, f"follow-done-{state}", monkeypatch=monkeypatch, state=state,
    )
    resp = _post(client, token, conv.id, asst.id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["suggestions"] == []
    assert captured["calls"] == 0


@pytest.mark.parametrize("extra", [
    {"document": {"title": "年假辦法"}},
    {"artifact": True},
    {"artifactRevision": "rev-1"},
    {"compare": True},
    {"source": "compare"},
    {"turn": "compare"},
    {"todos": [{"title": "查年假"}]},
    {"spans": [{"name": "a"}]},
    {"interrupt": {"reason": "等待"}},
    {"handoff_chain": [
        {"agent_id": "anila-router", "output_summary": "dispatch to hr-agent"},
        {"agent_id": "legal-agent"},
    ]},
])
def test_artifact_compare_and_agentic_turns_skip(client, db, monkeypatch, extra):
    name = "follow-skip-" + "-".join(extra.keys())
    _, token, captured, conv, _, asst = _ready(
        client, db, name, monkeypatch=monkeypatch, extra=extra,
    )
    resp = _post(client, token, conv.id, asst.id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["suggestions"] == []
    assert captured["calls"] == 0


def test_adopted_compare_answer_is_stamped_and_skipped(client, db, monkeypatch):
    make_user(db, username="follow-adopt")
    token = login(client, "follow-adopt")
    _summary(db, "follow-adopt-summary")
    captured = _install(monkeypatch, _LINES)
    text = "被採用的比較回答已經足夠長，不應該再產生建議問題。" * 2
    created = client.post(
        "/api/conversations/adopt",
        json={
            "title": "採用比較結果",
            "user_content": "同一問題",
            "assistant_content": text,
            "origin": "anila-ui",
            "assistant_metadata": {"note": "保留"},
        },
        headers=_auth(token),
    )
    assert created.status_code == 201, created.text
    body = created.json()
    mid = body["messages"][1]["id"]
    db.expire_all()
    stored = db.get(Message, mid)
    assert stored.metadata_["compare"] is True
    assert stored.metadata_["note"] == "保留"
    resp = _post(client, token, body["id"], mid)
    assert resp.status_code == 200, resp.text
    assert resp.json()["suggestions"] == []
    assert captured["calls"] == 0


def test_disabled_call_does_not_take_a_slot(client, db, monkeypatch):
    monkeypatch.setenv("FOLLOWUPS_MAX_CONCURRENCY", "1")
    _, token, captured, conv, _, asst = _ready(
        client, db, "follow-slot", monkeypatch=monkeypatch,
    )
    set_setting(db, "follow_ups.enabled", False)
    db.commit()
    assert _post(client, token, conv.id, asst.id).json()["suggestions"] == []
    assert captured["calls"] == 0
    assert try_acquire() is True
    release()


def test_cookie_session_requires_csrf_and_bearer_does_not(client, db, monkeypatch):
    user = make_user(db, username="follow-csrf")
    token = login(client, "follow-csrf")
    _summary(db, "follow-csrf-summary")
    _install(monkeypatch, _LINES)
    _, _, asst = _pair(db, user)
    blocked = client.post(
        f"/api/conversations/{asst.conversation_id}/messages/{asst.id}/follow-ups",
    )
    assert blocked.status_code == 403, blocked.text
    assert "CSRF" in blocked.json()["detail"]
    csrf = client.cookies.get("anila_csrf")
    allowed = client.post(
        f"/api/conversations/{asst.conversation_id}/messages/{asst.id}/follow-ups",
        headers={"X-CSRF-Token": csrf},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["suggestions"] == []
    bearer = _post(client, token, asst.conversation_id, asst.id)
    assert bearer.status_code == 200, bearer.text


def test_requires_login(client):
    resp = client.post("/api/conversations/1/messages/1/follow-ups")
    assert resp.status_code == 401


def test_main_chat_stream_does_not_grow_a_follow_up_call():
    root = Path(__file__).resolve().parents[1] / "app"
    for rel in ("api/proxy.py", "services/proxy/service.py", "api/conversations.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "follow_ups_service" not in text
        assert "/follow-ups" not in text
    from app.services.proxy.service import build_default_anila_meta

    assert build_default_anila_meta("m", detail="d")["follow_ups"] == []


@pytest.mark.parametrize("raw", [_LINES, "这个年假怎么算啊", "簡繁混合 这个問題", "NONE", "", "「病假需要附證明嗎」"])
@pytest.mark.parametrize("limit", [0, 1, 3])
def test_parser_fails_closed_for_all_unverified_text(raw, limit):
    assert parse_follow_ups(raw, "年假", limit=limit) == []


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("classified", [False, True])
@pytest.mark.parametrize("cached", [[], ["原有繁中建議"], ["这个年假怎么算啊"]])
def test_unavailable_suggestions_ignore_settings_and_never_replay_cache(
    client, db, monkeypatch, enabled, classified, cached,
):
    user, token, captured, conv, _, asst = _ready(
        client, db, "follow-unavailable", monkeypatch=monkeypatch,
        classified=classified, extra={"follow_ups": cached, "follow_ups_generated": True},
    )
    set_setting(db, "follow_ups.enabled", enabled)
    set_setting(db, "follow_ups.allow_classified", True)
    user.ui_settings = {"followUpsEnabled": True}
    db.commit()
    before = dict(asst.metadata_)
    for _ in range(2):
        response = _post(client, token, conv.id, asst.id)
        assert response.status_code == 200, response.text
        assert response.json() == {"suggestions": []}
    assert captured["calls"] == 0
    db.refresh(asst)
    assert asst.metadata_ == before  # 歷史只供稽核，不為了隱藏而改寫 DB。
    history = client.get(f"/api/conversations/{conv.id}?view=active", headers=_auth(token))
    assert history.status_code == 200, history.text
    visible = next(message for message in history.json()["messages"] if message["id"] == asst.id)
    assert "follow_ups" not in visible["metadata"]
    assert "follow_ups_generated" not in visible["metadata"]
    assert visible["metadata"]["anila_stream"] == before["anila_stream"]


def test_ui_settings_cannot_forge_server_follow_up_capability(client, db):
    make_user(db, username="follow-capability")
    token = login(client, "follow-capability")
    expected = {"available": False, "reason": "FOLLOW_UPS_UNAVAILABLE", "message": "追問建議目前未提供"}
    initial = client.get("/api/users/me/ui-settings", headers=_auth(token))
    assert initial.status_code == 200, initial.text
    assert initial.json()["capabilities"]["follow_ups"] == expected
    saved = client.put("/api/users/me/ui-settings", headers=_auth(token), json={
        "ui_settings": {"followUpsEnabled": True, "capabilities": {"follow_ups": {"available": True}}},
        "capabilities": {"follow_ups": {"available": True}},
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()["ui_settings"]["followUpsEnabled"] is True
    assert saved.json()["capabilities"]["follow_ups"] == expected
    reloaded = client.get("/api/users/me/ui-settings", headers=_auth(token))
    assert reloaded.json()["capabilities"]["follow_ups"] == expected


@pytest.mark.asyncio
async def test_policy_rechecked_before_second_cache_replay(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from app.services import follow_ups_service as service

    # 模擬等待名額期間能力關閉：第二個 gate 必須在第二次 cache read 前生效。
    availability = Mock(side_effect=[True, False])
    cached = Mock(return_value=None)
    generate = AsyncMock(return_value=["不可回傳的建議"])
    monkeypatch.setattr(service, "follow_ups_available", availability)
    monkeypatch.setattr(service, "_cached", cached)
    monkeypatch.setattr(service, "_should_skip", lambda *args: False)
    monkeypatch.setattr(service, "_last_user_message", lambda *args: SimpleNamespace(content="年假問題"))
    monkeypatch.setattr(service, "resolve_followup_model", lambda *args, **kwargs: object())
    monkeypatch.setattr(service, "_generate", generate)
    message = SimpleNamespace(role="assistant", metadata_={"anila_stream": {"state": "complete"}})
    result = await service.suggest_follow_ups(SimpleNamespace(refresh=lambda message: None), object(),
                                            SimpleNamespace(classified=False), message)
    assert result == []
    assert cached.call_count == 1
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_generation_and_persistence_also_fail_closed(monkeypatch):
    from unittest.mock import AsyncMock
    from app.services import follow_ups_service as service

    complete = AsyncMock(side_effect=AssertionError("no model requests while unavailable"))
    monkeypatch.setattr("app.services.internal_llm.complete_chat", complete)
    assert await service._generate(None, None, None, None, "任意文字", None) == []
    assert service._persist(None, 1, ["任意簡體追問 这个問題"]) == []
    complete.assert_not_awaited()


@pytest.mark.parametrize("method", ["post", "put"])
def test_message_write_response_does_not_echo_cached_follow_ups(client, db, monkeypatch, method):
    _, token, captured, conv, _, asst = _ready(
        client, db, "follow-write-cache", monkeypatch=monkeypatch,
    )
    metadata = {"follow_ups": ["这个旧问题"], "follow_ups_generated": True,
                "anila_stream": {"state": "complete"}, "audit_note": "保留"}
    url = f"/api/conversations/{conv.id}/messages" + (f"/{asst.id}" if method == "put" else "")
    payload = {"content": _ANSWER, "metadata": metadata}
    if method == "post":
        payload["role"] = "assistant"
    response = getattr(client, method)(url, headers=_auth(token), json=payload)
    assert response.status_code == (201 if method == "post" else 200), response.text
    visible = response.json()
    assert "follow_ups" not in visible["metadata"]
    assert "follow_ups_generated" not in visible["metadata"]
    assert visible["metadata"]["audit_note"] == "保留"
    db.expire_all()
    stored = db.get(Message, visible["id"])
    assert stored.metadata_["follow_ups"] == metadata["follow_ups"]
    assert stored.metadata_["follow_ups_generated"] is True
    assert captured["calls"] == 0
