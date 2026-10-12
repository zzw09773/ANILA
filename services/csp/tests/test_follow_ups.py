"""建議下一個問題：解析、權限、跳過、快取、並行與用量。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.models.conversation import Conversation, ConversationShare, ConversationUserMeta
from app.models.message import Message
from app.models.model_registry import ModelRegistry
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
from app.services.internal_llm import InternalCompletionError
from app.services.service_token_envelope import encode_service_token_envelope
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


def test_parser_strips_numbering_quotes_blanks_and_duplicates():
    assert parse_follow_ups(_LINES, "年假還能請幾天", limit=3) == _PARSED
    wrapped = "「特休可以換成錢嗎」\n'病假需要附證明嗎'\n* 剩餘天數在哪裡查"
    assert parse_follow_ups(wrapped, "年假還能請幾天", limit=3) == [
        "特休可以換成錢嗎",
        "病假需要附證明嗎",
        "剩餘天數在哪裡查",
    ]


def test_parser_none_simplified_length_and_near_duplicates():
    assert parse_follow_ups("NONE\n1. NONE。\n", "年假還能請幾天", limit=3) == []
    mixed = "NONE\n特休可以換成錢嗎\n"
    assert parse_follow_ups(mixed, "年假還能請幾天", limit=3) == ["特休可以換成錢嗎"]
    assert parse_follow_ups("这个年假怎么算啊\n特休可以換成錢嗎", "年假還能請幾天", limit=3) == [
        "特休可以換成錢嗎",
    ]
    assert parse_follow_ups("皇后的後面怎麼走", "起始問題", limit=3) == ["皇后的後面怎麼走"]
    assert parse_follow_ups("台北的天氣如何", "起始問題", limit=3) == []
    assert parse_follow_ups("請問年假還能請幾天", "起始問題", limit=3) == ["請問年假還能請幾天"]
    assert parse_follow_ups("問" * 41 + "\n" + "問" * 40 + "\n然後呢\n那下一步", "起始", limit=4) == [
        "問" * 40,
        "那下一步",
    ]
    assert parse_follow_ups("年假還能請幾天？", "  年假還能請幾天  ", limit=3) == []
    assert parse_follow_ups("  What is ANILA? ", "what is anila", limit=3) == []
    longer = "甲" * 20
    assert parse_follow_ups("甲" * 17, longer, limit=3) == []
    assert parse_follow_ups("甲" * 16, longer, limit=3) == ["甲" * 16]
    assert parse_follow_ups("特休可以換成錢嗎\n特休可以換成錢嗎", "起始問題", limit=3) == [
        "特休可以換成錢嗎",
    ]


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


def test_follow_up_settings_default_on_off_and_three(db):
    assert get_setting(db, "follow_ups.enabled") is True
    assert get_setting(db, "follow_ups.allow_classified") is False
    assert get_setting(db, "follow_ups.max") == 3


def test_happy_path_payload_cache_and_reload(client, db, monkeypatch):
    user, token, captured, conv, _, asst = _ready(client, db, "follow-happy", monkeypatch=monkeypatch)
    first = _post(client, token, conv.id, asst.id)
    assert first.status_code == 200, first.text
    assert first.json() == {"suggestions": _PARSED}
    assert captured["calls"] == 1
    assert captured["model"] == "follow-happy-summary"
    assert captured["kwargs"]["user_id"] == user.id
    assert captured["kwargs"]["on_behalf_of_user"] is True
    assert captured["kwargs"]["request_type_override"] == "follow_ups"
    assert captured["kwargs"]["record_usage"] is True
    assert captured["kwargs"]["conversation_id"] == str(conv.id)
    assert isinstance(captured["kwargs"]["deadline"], float)
    payload = captured["payload"]
    assert payload["temperature"] == 0.3
    assert payload["max_tokens"] == 120
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert [item["role"] for item in payload["messages"]] == ["system", "user"]
    assert payload["messages"][0]["content"] == FOLLOW_UP_PROMPT
    assert "年假還能請幾天" in payload["messages"][1]["content"]
    assert _ANSWER in payload["messages"][1]["content"]

    set_setting(db, "follow_ups.enabled", False)
    db.commit()
    second = _post(client, token, conv.id, asst.id)
    assert second.status_code == 200, second.text
    assert second.json()["suggestions"] == _PARSED
    assert captured["calls"] == 1

    db.expire_all()
    stored = db.get(Message, asst.id)
    assert stored.metadata_["follow_ups"] == _PARSED
    assert stored.metadata_["follow_ups_generated"] is True
    assert stored.metadata_["anila_stream"]["state"] == "complete"
    reloaded = client.get(f"/api/conversations/{conv.id}?view=active", headers=_auth(token))
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json()["messages"][1]["metadata"]["follow_ups"] == _PARSED


def test_empty_skeleton_is_not_a_cache_hit(client, db, monkeypatch):
    _, token, captured, conv, _, asst = _ready(
        client, db, "follow-skeleton", monkeypatch=monkeypatch, extra={"follow_ups": []},
    )
    resp = _post(client, token, conv.id, asst.id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["suggestions"] == _PARSED
    assert captured["calls"] == 1


def test_none_is_cached_and_not_regenerated(client, db, monkeypatch):
    _, token, captured, conv, _, asst = _ready(
        client, db, "follow-none", monkeypatch=monkeypatch, content="NONE",
    )
    first = _post(client, token, conv.id, asst.id)
    assert first.json()["suggestions"] == []
    second = _post(client, token, conv.id, asst.id)
    assert second.json()["suggestions"] == []
    assert captured["calls"] == 1
    db.expire_all()
    assert db.get(Message, asst.id).metadata_["follow_ups_generated"] is True


def test_echoed_question_is_dropped_and_cached(client, db, monkeypatch):
    _, token, captured, conv, _, asst = _ready(
        client, db, "follow-echo", monkeypatch=monkeypatch, content="年假還能請幾天？",
    )
    assert _post(client, token, conv.id, asst.id).json()["suggestions"] == []
    assert _post(client, token, conv.id, asst.id).json()["suggestions"] == []
    assert captured["calls"] == 1


def test_max_setting_trims_before_cache(client, db, monkeypatch):
    _, token, captured, conv, _, asst = _ready(client, db, "follow-max", monkeypatch=monkeypatch)
    set_setting(db, "follow_ups.max", 1)
    db.commit()
    first = _post(client, token, conv.id, asst.id)
    assert first.json()["suggestions"] == ["特休可以遞延到明年嗎"]
    set_setting(db, "follow_ups.max", 10)
    db.commit()
    second = _post(client, token, conv.id, asst.id)
    assert second.json()["suggestions"] == ["特休可以遞延到明年嗎"]
    assert captured["calls"] == 1


def test_input_is_truncated_and_omits_attachment_text(client, db, monkeypatch):
    question = "問" * 2000 + "尾問不該出現"
    answer = "答" * 2000 + "尾答不該出現"
    _, token, captured, conv, _, asst = _ready(
        client,
        db,
        "follow-clip",
        monkeypatch=monkeypatch,
        question=question,
        answer=answer,
        extra={"attachment_text": "附件全文不該進追問"},
    )
    resp = _post(client, token, conv.id, asst.id)
    assert resp.status_code == 200, resp.text
    body = captured["payload"]["messages"][1]["content"]
    assert "問" * 2000 in body
    assert "答" * 2000 in body
    assert "尾問不該出現" not in body
    assert "尾答不該出現" not in body
    assert "附件全文不該進追問" not in body
    assert len(captured["payload"]["messages"]) == 2


def test_parent_user_wins_over_a_later_message(client, db, monkeypatch):
    user = make_user(db, username="follow-parent")
    token = login(client, "follow-parent")
    _summary(db, "follow-parent-summary")
    captured = _install(monkeypatch, "特休可以換成錢嗎")
    conv = Conversation(user_id=user.id, title="追問", origin="anila-ui")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    first = Message(
        conversation_id=conv.id, role="user", content="沿父節點的這個問題",
        classification_level="無機密",
    )
    db.add(first)
    db.flush()
    second = Message(
        conversation_id=conv.id, parent_id=first.id, role="user",
        content="這則比較新但不在路徑上", classification_level="無機密",
    )
    db.add(second)
    db.flush()
    asst = Message(
        conversation_id=conv.id, parent_id=first.id, role="assistant", content=_ANSWER,
        metadata_={"anila_stream": {"state": "complete"}}, classification_level="無機密",
    )
    db.add(asst)
    db.commit()
    resp = _post(client, token, conv.id, asst.id)
    assert resp.status_code == 200, resp.text
    text = captured["payload"]["messages"][1]["content"]
    assert "沿父節點的這個問題" in text
    assert "這則比較新但不在路徑上" not in text


def test_orphan_assistant_uses_latest_prior_user(client, db, monkeypatch):
    user = make_user(db, username="follow-orphan")
    token = login(client, "follow-orphan")
    _summary(db, "follow-orphan-summary")
    captured = _install(monkeypatch, "特休可以換成錢嗎")
    conv = Conversation(user_id=user.id, title="追問", origin="anila-ui")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    early = Message(
        conversation_id=conv.id, role="user", content="較早的問題",
        classification_level="無機密",
    )
    later = Message(
        conversation_id=conv.id, role="user", content="較新的問題",
        classification_level="無機密",
    )
    db.add_all([early, later])
    db.flush()
    asst = Message(
        conversation_id=conv.id, parent_id=None, role="assistant", content=_ANSWER,
        metadata_={"anila_stream": {"state": "complete"}}, classification_level="無機密",
    )
    db.add(asst)
    db.commit()
    assert _post(client, token, conv.id, asst.id).status_code == 200
    text = captured["payload"]["messages"][1]["content"]
    assert "較新的問題" in text
    assert "較早的問題" not in text


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


def test_answer_shorter_than_40_skips_and_40_proceeds(client, db, monkeypatch):
    _, token, captured, conv, _, asst = _ready(
        client, db, "follow-short", monkeypatch=monkeypatch, answer=f"  {'答' * 39}  ",
    )
    short = _post(client, token, conv.id, asst.id)
    assert short.json()["suggestions"] == []
    assert captured["calls"] == 0
    _, token2, captured2, conv2, _, asst2 = _ready(
        client, db, "follow-forty", monkeypatch=monkeypatch, answer="答" * 40,
    )
    ok = _post(client, token2, conv2.id, asst2.id)
    assert ok.json()["suggestions"] == _PARSED
    assert captured2["calls"] == 1


def test_no_stream_envelope_is_treated_as_complete(client, db, monkeypatch):
    _, token, captured, conv, _, asst = _ready(
        client, db, "follow-envelope", monkeypatch=monkeypatch, state=None,
    )
    resp = _post(client, token, conv.id, asst.id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["suggestions"] == _PARSED
    assert captured["calls"] == 1


def test_governance_off_and_user_opt_out_skip_the_model(client, db, monkeypatch):
    _, token, captured, conv, _, asst = _ready(
        client, db, "follow-govern", monkeypatch=monkeypatch,
    )
    set_setting(db, "follow_ups.enabled", False)
    db.commit()
    assert _post(client, token, conv.id, asst.id).json()["suggestions"] == []
    assert captured["calls"] == 0
    set_setting(db, "follow_ups.enabled", True)
    db.commit()
    assert _post(client, token, conv.id, asst.id).json()["suggestions"] == _PARSED
    assert captured["calls"] == 1

    user = make_user(db, username="follow-opt")
    user.ui_settings = {"followUpsEnabled": False}
    db.commit()
    token_opt = login(client, "follow-opt")
    _summary(db, "follow-opt-summary")
    captured_opt = _install(monkeypatch, _LINES)
    conv_opt, _, asst_opt = _pair(db, user)
    assert _post(client, token_opt, conv_opt.id, asst_opt.id).json()["suggestions"] == []
    user.ui_settings = {"skillAutoApply": False}
    db.commit()
    assert _post(client, token_opt, conv_opt.id, asst_opt.id).json()["suggestions"] == _PARSED
    user.ui_settings = {"followUpsEnabled": True}
    db.commit()
    conv_on, _, asst_on = _pair(db, user)
    assert _post(client, token_opt, conv_on.id, asst_on.id).status_code == 200
    user.ui_settings = {"followUpsEnabled": 0}
    db.commit()
    conv_zero, _, asst_zero = _pair(db, user)
    assert _post(client, token_opt, conv_zero.id, asst_zero.id).json()["suggestions"] == _PARSED
    assert captured_opt["calls"] == 3


def test_classified_skips_unless_allowed_and_then_local_only(client, db, monkeypatch):
    user = make_user(db, username="follow-secret")
    token = login(client, "follow-secret")
    remote = _summary(db, "follow-secret-remote", internal=False)
    captured = _install(monkeypatch, "特休可以換成錢嗎")
    conv, _, asst = _pair(db, user, classified=True)
    blocked = _post(client, token, conv.id, asst.id)
    assert blocked.json()["suggestions"] == []
    assert captured["calls"] == 0

    set_setting(db, "follow_ups.allow_classified", True)
    db.commit()
    still_remote = _post(client, token, conv.id, asst.id)
    assert still_remote.json()["suggestions"] == []
    assert captured["calls"] == 0

    remote.is_active = False
    db.commit()
    _primary(db, "follow-secret-local", internal=True)
    allowed = _post(client, token, conv.id, asst.id)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["suggestions"] == ["特休可以換成錢嗎"]
    assert captured["calls"] == 1
    assert captured["model"] == "follow-secret-local"
    assert captured["kwargs"]["record_usage"] is False
    assert captured["kwargs"]["conversation_id"] is None
    assert captured["kwargs"]["on_behalf_of_user"] is True


def test_classified_prefers_internal_summary_over_remote(client, db, monkeypatch):
    user = make_user(db, username="follow-local-summary")
    token = login(client, "follow-local-summary")
    _summary(db, "follow-local-summary-model", internal=True)
    _primary(db, "follow-local-summary-primary", internal=False)
    captured = _install(monkeypatch, "特休可以換成錢嗎")
    set_setting(db, "follow_ups.allow_classified", True)
    db.commit()
    conv, _, asst = _pair(db, user, classified=True)
    resp = _post(client, token, conv.id, asst.id)
    assert resp.json()["suggestions"] == ["特休可以換成錢嗎"]
    assert captured["model"] == "follow-local-summary-model"
    assert captured["kwargs"]["record_usage"] is False


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


def test_fence_tool_call_single_handoff_and_compared_tag(client, db, monkeypatch):
    _, token, captured, conv, _, asst = _ready(
        client, db, "follow-fence", monkeypatch=monkeypatch,
        answer=_ANSWER + "\n```html\n<h1>年假</h1>\n```",
    )
    assert _post(client, token, conv.id, asst.id).json()["suggestions"] == []
    _, token_py, captured_py, conv_py, _, asst_py = _ready(
        client, db, "follow-python", monkeypatch=monkeypatch,
        answer=_ANSWER + "\n```python\nprint('年假')\n```",
        extra={"tool_calls": [{"name": "search"}], "handoff_chain": [
            {"agent_id": "anila-router", "output_summary": "dispatch to hr-agent"},
        ]},
    )
    db.add(ConversationUserMeta(
        user_id=conv_py.user_id, conversation_id=conv_py.id, user_tags=["compared"],
    ))
    db.commit()
    resp = _post(client, token_py, conv_py.id, asst_py.id)
    assert resp.json()["suggestions"] == _PARSED
    assert captured["calls"] == 0
    assert captured_py["calls"] == 1


def test_adopted_compare_answer_is_stamped_and_skipped(client, db, monkeypatch):
    user = make_user(db, username="follow-adopt")
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


def test_timeout_upstream_and_quota_return_empty_without_cache(client, db, monkeypatch):
    monkeypatch.setenv("FOLLOWUPS_TIMEOUT_S", "0.2")
    monkeypatch.setenv("FOLLOWUPS_MAX_CONCURRENCY", "1")

    async def slow():
        await asyncio.sleep(2)
        return "特休可以換成錢嗎"

    _, token, captured, conv, _, asst = _ready(
        client, db, "follow-timeout", monkeypatch=monkeypatch, content=slow,
    )
    timed = _post(client, token, conv.id, asst.id)
    assert timed.status_code == 200, timed.text
    assert timed.json()["suggestions"] == []
    assert try_acquire() is True
    release()
    db.expire_all()
    assert db.get(Message, asst.id).metadata_.get("follow_ups_generated") is not True

    monkeypatch.delenv("FOLLOWUPS_TIMEOUT_S", raising=False)

    async def later():
        return "特休可以換成錢嗎"

    again = _install(monkeypatch, later)
    retried = _post(client, token, conv.id, asst.id)
    assert retried.json()["suggestions"] == ["特休可以換成錢嗎"]
    assert again["calls"] == 1
    assert captured["calls"] == 1

    _, token5, captured5, conv5, _, asst5 = _ready(
        client, db, "follow-5xx", monkeypatch=monkeypatch,
        content=InternalCompletionError(502),
    )
    failed = _post(client, token5, conv5.id, asst5.id)
    assert failed.status_code == 200, failed.text
    assert failed.json()["suggestions"] == []
    assert try_acquire() is True
    release()
    db.expire_all()
    assert db.get(Message, asst5.id).metadata_.get("follow_ups_generated") is not True
    recovered = _install(monkeypatch, "特休可以換成錢嗎")
    assert _post(client, token5, conv5.id, asst5.id).json()["suggestions"] == ["特休可以換成錢嗎"]
    assert recovered["calls"] == 1

    _, token429, captured429, conv429, _, asst429 = _ready(
        client, db, "follow-quota", monkeypatch=monkeypatch,
        content=InternalCompletionError(429, code="quota_exceeded", message="已達上限"),
    )
    quota = _post(client, token429, conv429.id, asst429.id)
    assert quota.status_code == 200, quota.text
    assert quota.json()["suggestions"] == []
    assert captured429["calls"] == 1


def test_semaphore_full_returns_empty_immediately(client, db, monkeypatch):
    monkeypatch.setenv("FOLLOWUPS_MAX_CONCURRENCY", "1")
    _, token, captured, conv, _, asst = _ready(
        client, db, "follow-sem", monkeypatch=monkeypatch,
    )
    assert try_acquire() is True
    try:
        resp = _post(client, token, conv.id, asst.id)
        assert resp.status_code == 200, resp.text
        assert resp.json()["suggestions"] == []
        assert captured["calls"] == 0
    finally:
        release()
    db.expire_all()
    assert db.get(Message, asst.id).metadata_.get("follow_ups_generated") is not True
    opened = _post(client, token, conv.id, asst.id)
    assert opened.json()["suggestions"] == _PARSED
    assert captured["calls"] == 1


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


def test_parser_failure_is_empty_and_not_cached(client, db, monkeypatch):
    from app.services import follow_ups_service

    class Boom:
        def convert(self, text):
            raise RuntimeError("boom")

    monkeypatch.setattr(follow_ups_service, "_s2t", Boom())
    _, token, captured, conv, _, asst = _ready(
        client, db, "follow-opencc", monkeypatch=monkeypatch,
    )
    failed = _post(client, token, conv.id, asst.id)
    assert failed.json()["suggestions"] == []
    assert captured["calls"] == 1
    db.expire_all()
    assert db.get(Message, asst.id).metadata_.get("follow_ups_generated") is not True
    monkeypatch.setattr(follow_ups_service, "_s2t", None)
    again = _install(monkeypatch, _LINES)
    assert _post(client, token, conv.id, asst.id).json()["suggestions"] == _PARSED
    assert again["calls"] == 1


def test_summary_falls_back_to_router_primary(client, db, monkeypatch):
    user = make_user(db, username="follow-fallback")
    token = login(client, "follow-fallback")
    primary = _primary(db, "follow-fallback-primary")
    captured = _install(monkeypatch, "特休可以換成錢嗎")
    conv, _, asst = _pair(db, user)
    first = _post(client, token, conv.id, asst.id)
    assert first.json()["suggestions"] == ["特休可以換成錢嗎"]
    assert captured["model"] == "follow-fallback-primary"

    primary.is_router_primary = False
    db.commit()
    user2 = make_user(db, username="follow-no-model")
    token2 = login(client, "follow-no-model")
    conv2, _, asst2 = _pair(db, user2)
    missed = _post(client, token2, conv2.id, asst2.id)
    assert missed.status_code == 200, missed.text
    assert missed.json()["suggestions"] == []
    assert captured["calls"] == 1


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
    assert allowed.json()["suggestions"] == _PARSED
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


def test_usage_request_type_is_follow_ups(client, db, monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "summary.test")
    from app.config import settings

    monkeypatch.setattr(settings, "MODEL_GATEWAY_API_KEY", "", raising=False)
    user = make_user(db, username="follow-usage")
    token = login(client, "follow-usage")
    row = ModelRegistry(
        name="follow-usage-summary",
        display_name="摘要",
        model_type="llm",
        endpoint_url="http://summary.test:4000/v1",
        api_version="v1",
        protocol="openai_compatible",
        is_active=True,
        api_key_secret_ref=encode_service_token_envelope("sk-follow-up-key"),
    )
    db.add(row)
    db.commit()
    db.add(ModelRole(role="summary", model_id=row.id))
    db.commit()
    conv, _, asst = _pair(db, user)
    captured: dict = {}

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = "特休可以換成錢嗎"

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"content": "特休可以換成錢嗎"}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            }

    class _Client:
        def __init__(self, *args, **kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, headers=None):
            captured["calls"] = captured.get("calls", 0) + 1
            captured["body"] = json
            return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    enqueued: list[dict] = []

    async def fake_enqueue(**kwargs):
        enqueued.append(kwargs)

    monkeypatch.setattr("app.services.proxy.service.enqueue_usage", fake_enqueue)
    resp = _post(client, token, conv.id, asst.id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["suggestions"] == ["特休可以換成錢嗎"]
    assert captured["calls"] == 1
    assert captured["body"]["max_tokens"] == 120
    assert captured["body"]["temperature"] == 0.3
    assert captured["body"]["chat_template_kwargs"]["enable_thinking"] is False
    assert len(enqueued) == 1
    assert enqueued[0]["request_type"] == "follow_ups"
    assert enqueued[0]["usage_kind"] == "inference"
    assert enqueued[0]["user_id"] == user.id
    assert enqueued[0]["conversation_id"] == str(conv.id)
    assert len(enqueued[0]["request_type"]) <= 20
    cached = _post(client, token, conv.id, asst.id)
    assert cached.json()["suggestions"] == ["特休可以換成錢嗎"]
    assert captured["calls"] == 1


def test_classified_allow_does_not_enqueue_usage(client, db, monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "summary.test")
    from app.config import settings

    monkeypatch.setattr(settings, "MODEL_GATEWAY_API_KEY", "", raising=False)
    user = make_user(db, username="follow-usage-secret")
    token = login(client, "follow-usage-secret")
    row = ModelRegistry(
        name="follow-usage-local",
        display_name="本地摘要",
        model_type="llm",
        endpoint_url="http://summary.test:4000/v1",
        api_version="v1",
        protocol="openai_compatible",
        is_active=True,
        is_internal=True,
        api_key_secret_ref=encode_service_token_envelope("sk-follow-up-local"),
    )
    db.add(row)
    db.commit()
    db.add(ModelRole(role="summary", model_id=row.id))
    db.commit()
    set_setting(db, "follow_ups.allow_classified", True)
    db.commit()
    conv, _, asst = _pair(db, user, classified=True)
    calls = {"n": 0}

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = "特休可以換成錢嗎"

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"content": "特休可以換成錢嗎"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 4, "total_tokens": 8},
            }

    class _Client:
        def __init__(self, *args, **kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, headers=None):
            calls["n"] += 1
            return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    enqueued: list[dict] = []

    async def fake_enqueue(**kwargs):
        enqueued.append(kwargs)

    monkeypatch.setattr("app.services.proxy.service.enqueue_usage", fake_enqueue)
    resp = _post(client, token, conv.id, asst.id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["suggestions"] == ["特休可以換成錢嗎"]
    assert calls["n"] == 1
    assert enqueued == []
    cached = _post(client, token, conv.id, asst.id)
    assert cached.json()["suggestions"] == ["特休可以換成錢嗎"]
    assert calls["n"] == 1
    assert enqueued == []
