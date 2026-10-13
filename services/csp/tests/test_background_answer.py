# -*- coding: utf-8 -*-
"""完整回答在背景完成，並用既有通知告訴送出的人。"""
from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from sqlalchemy.orm import sessionmaker

from app.models.conversation import Conversation
from app.models.handoff import Notification
from app.models.message import Message
from app.models.router_model_grant import RouterModelGrant
from app.services.answer_delivery import (
    BACKGROUND_FAILURE,
    BACKGROUND_PLACEHOLDER,
    FULL_ANSWER_SYSTEM,
    SHORT_ANSWER_SYSTEM,
    apply_short_answer,
)
from app.services.background_answer import (
    enqueue_background_answer,
    run_background_answer,
)
from app.utils.security import create_access_token
from tests.conftest import make_model, make_user


def _bearer(user) -> dict:
    token = create_access_token({
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "tv": user.token_version,
    })
    return {"Authorization": f"Bearer {token}"}


def _ready_model(db, name="composer-test"):
    model = make_model(db, name=name)
    model.router_enabled = True
    model.display_name = "Composer"
    model.model_type = "llm"
    db.add(model)
    db.flush()
    db.add(RouterModelGrant(model_id=model.id, scope_type="all"))
    db.commit()
    db.refresh(model)
    return model


def _conv(db, user, model) -> Conversation:
    conv = Conversation(
        user_id=user.id,
        title="週報草稿",
        router_model_id=model.id,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def test_short_answer_instruction_is_header_driven_and_not_repeated():
    body = {"messages": [{"role": "user", "content": "預算多少"}]}
    apply_short_answer(body, None)
    assert body["messages"] == [{"role": "user", "content": "預算多少"}]
    apply_short_answer(body, "short")
    apply_short_answer(body, "short")
    assert body["messages"][0] == {"role": "system", "content": SHORT_ANSWER_SYSTEM}
    assert body["messages"][1]["content"] == "預算多少"


def test_enqueue_reserves_a_placeholder_without_calling_a_model(db):
    user = make_user(db, username="bg-alice")
    model = _ready_model(db)
    conv = _conv(db, user, model)
    _user, assistant, _gap = enqueue_background_answer(
        db, conv.id, user, content="請說明這週進度",
    )
    assert assistant.content == BACKGROUND_PLACEHOLDER
    assert assistant.model_name == model.name
    assert assistant.metadata_["answer_delivery"] == "background"
    assert assistant.metadata_["anila_stream"]["state"] == "reserved"
    assert "writer" in assistant.metadata_["anila_stream"]


@pytest.mark.asyncio
async def test_run_writes_the_answer_and_one_notification(db, monkeypatch):
    user = make_user(db, username="bg-bob")
    model = _ready_model(db, name="grok-test")
    conv = _conv(db, user, model)
    _user_msg, assistant, _gap = enqueue_background_answer(
        db, conv.id, user, content="請完整說明",
    )

    async def fake_complete(db, user, model, messages, *, conversation_id):
        assert model.name == "grok-test"
        assert conversation_id == conv.id
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == FULL_ANSWER_SYSTEM
        assert messages[-1]["content"] == "請完整說明"
        assert BACKGROUND_PLACEHOLDER not in [m["content"] for m in messages]
        return "步驟一：先核對數字。"

    monkeypatch.setattr(
        "app.services.background_answer.complete_chat", fake_complete,
    )
    await run_background_answer(db, assistant.id)
    await run_background_answer(db, assistant.id)

    saved = db.get(Message, assistant.id)
    assert saved.content == "步驟一：先核對數字。"
    assert saved.metadata_["anila_stream"]["state"] == "complete"
    notes = (
        db.query(Notification)
        .filter(Notification.user_id == user.id, Notification.type == "answer_ready")
        .all()
    )
    assert len(notes) == 1
    assert notes[0].title == "完整回答已完成"
    assert "週報草稿" in notes[0].body
    assert notes[0].payload["conversation_id"] == conv.id
    assert notes[0].payload["message_id"] == assistant.id


@pytest.mark.asyncio
async def test_model_failure_notifies_without_leaking_the_error(db, monkeypatch):
    user = make_user(db, username="bg-cara")
    model = _ready_model(db, name="cursor-test")
    conv = _conv(db, user, model)
    _user_msg, assistant, _gap = enqueue_background_answer(
        db, conv.id, user, content="請完整說明",
    )

    async def explode(*_args, **_kwargs):
        raise RuntimeError("sk-secret upstream boom")

    monkeypatch.setattr(
        "app.services.background_answer.complete_chat", explode,
    )
    await run_background_answer(db, assistant.id)

    saved = db.get(Message, assistant.id)
    assert saved.content == BACKGROUND_FAILURE
    assert "sk-secret" not in saved.content
    assert saved.metadata_["anila_stream"]["state"] == "failed"
    note = (
        db.query(Notification)
        .filter(Notification.user_id == user.id, Notification.type == "answer_failed")
        .one()
    )
    assert "sk-secret" not in note.body
    assert "沒有完成" in note.title


def test_other_user_cannot_enqueue(db):
    owner = make_user(db, username="bg-owner")
    stranger = make_user(db, username="bg-stranger")
    model = _ready_model(db, name="composer-owned")
    conv = _conv(db, owner, model)
    with pytest.raises(Exception) as caught:
        enqueue_background_answer(db, conv.id, stranger, content="偷看")
    assert getattr(caught.value, "status_code", None) == 404


def test_missing_model_does_not_leave_a_message(db):
    user = make_user(db, username="bg-nomodel")
    conv = Conversation(user_id=user.id, title="還沒選模型")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    with pytest.raises(Exception) as caught:
        enqueue_background_answer(db, conv.id, user, content="請回答")
    assert getattr(caught.value, "status_code", None) == 409
    assert db.query(Message).filter(Message.conversation_id == conv.id).count() == 0


def test_api_returns_immediately_and_notifies(client, db, db_engine, monkeypatch):
    user = make_user(db, username="bg-api")
    model = _ready_model(db, name="composer-api")
    conv = _conv(db, user, model)
    Session = sessionmaker(bind=db_engine, expire_on_commit=False)

    async def fake_complete(*_args, **_kwargs):
        return "背景完成的全文。"

    monkeypatch.setattr(
        "app.services.background_answer.session_factory", Session,
    )
    monkeypatch.setattr(
        "app.services.background_answer.complete_chat", fake_complete,
    )
    resp = client.post(
        f"/api/conversations/{conv.id}/background-answers",
        headers=_bearer(user),
        json={"content": "請寫完整說明"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["user"]["content"] == "請寫完整說明"
    assert "writer" not in (body["assistant"]["metadata"].get("anila_stream") or {})
    follow = Session()
    try:
        saved = follow.get(Message, body["assistant"]["id"])
        assert saved.content == "背景完成的全文。"
        note = (
            follow.query(Notification)
            .filter(Notification.user_id == user.id, Notification.type == "answer_ready")
            .one()
        )
        assert note.payload["conversation_id"] == conv.id
    finally:
        follow.close()

    listed = client.get("/api/notifications?unread_only=true", headers=_bearer(user))
    assert listed.status_code == 200
    assert any(row["type"] == "answer_ready" for row in listed.json())
