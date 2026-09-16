"""POST /api/attachments/bind — orphan → conversation, without stealing."""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.models.attachment import Attachment
from app.models.conversation import Conversation
from app.models.message import Message
from tests.conftest import login, make_user


def _make_conv(db, user, title="bind-test") -> Conversation:
    conv = Conversation(user_id=user.id, title=title)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def _row(
    db,
    *,
    user,
    conv=None,
    filename="note.md",
    extract_status="ok",
) -> Attachment:
    att = Attachment(
        reference_id=str(uuid.uuid4()),
        conversation_id=conv.id if conv is not None else None,
        uploaded_by=user.id,
        filename=filename,
        content_type="text/markdown",
        size_bytes=12,
        storage_path=f"{user.id}/{uuid.uuid4()}.md",
        extract_status=extract_status,
        extracted_text="hello bind",
        token_count=2,
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return att


def _auth(client: TestClient, username: str) -> dict:
    token = login(client, username=username)
    return {"Authorization": f"Bearer {token}"}


def test_bind_orphan_sets_conversation_id(client: TestClient, db):
    user = make_user(db, username="bind-owner")
    conv = _make_conv(db, user)
    att = _row(db, user=user)

    resp = client.post(
        "/api/attachments/bind",
        headers=_auth(client, "bind-owner"),
        json={
            "conversation_id": conv.id,
            "reference_ids": [att.reference_id],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["conversation_id"] == conv.id
    assert body["attachments"][0]["reference_id"] == att.reference_id
    assert body["attachments"][0]["conversation_id"] == conv.id

    db.refresh(att)
    assert att.conversation_id == conv.id


def test_bind_same_conversation_is_idempotent(client: TestClient, db):
    user = make_user(db, username="bind-idem")
    conv = _make_conv(db, user)
    att = _row(db, user=user, conv=conv)

    payload = {
        "conversation_id": conv.id,
        "reference_ids": [att.reference_id],
    }
    first = client.post(
        "/api/attachments/bind",
        headers=_auth(client, "bind-idem"),
        json=payload,
    )
    second = client.post(
        "/api/attachments/bind",
        headers=_auth(client, "bind-idem"),
        json=payload,
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    db.refresh(att)
    assert att.conversation_id == conv.id
    assert (
        db.query(Attachment)
        .filter(Attachment.reference_id == att.reference_id)
        .count()
        == 1
    )


def test_bind_rejects_foreign_attachment(client: TestClient, db):
    owner = make_user(db, username="bind-att-owner")
    attacker = make_user(db, username="bind-att-attacker")
    conv = _make_conv(db, attacker)
    att = _row(db, user=owner)

    resp = client.post(
        "/api/attachments/bind",
        headers=_auth(client, "bind-att-attacker"),
        json={
            "conversation_id": conv.id,
            "reference_ids": [att.reference_id],
        },
    )
    assert resp.status_code == 403
    db.refresh(att)
    assert att.conversation_id is None


def test_bind_rejects_stealing_already_bound_other_conversation(
    client: TestClient, db,
):
    user = make_user(db, username="bind-steal")
    original = _make_conv(db, user, title="original")
    other = _make_conv(db, user, title="other")
    att = _row(db, user=user, conv=original)

    resp = client.post(
        "/api/attachments/bind",
        headers=_auth(client, "bind-steal"),
        json={
            "conversation_id": other.id,
            "reference_ids": [att.reference_id],
        },
    )
    assert resp.status_code == 409
    db.refresh(att)
    assert att.conversation_id == original.id


def test_bind_rejects_foreign_conversation(client: TestClient, db):
    owner = make_user(db, username="bind-conv-owner")
    attacker = make_user(db, username="bind-conv-attacker")
    conv = _make_conv(db, owner)
    att = _row(db, user=attacker)

    resp = client.post(
        "/api/attachments/bind",
        headers=_auth(client, "bind-conv-attacker"),
        json={
            "conversation_id": conv.id,
            "reference_ids": [att.reference_id],
        },
    )
    assert resp.status_code == 404
    db.refresh(att)
    assert att.conversation_id is None


def test_bind_missing_reference_is_404(client: TestClient, db):
    user = make_user(db, username="bind-miss")
    conv = _make_conv(db, user)
    resp = client.post(
        "/api/attachments/bind",
        headers=_auth(client, "bind-miss"),
        json={
            "conversation_id": conv.id,
            "reference_ids": [str(uuid.uuid4())],
        },
    )
    assert resp.status_code == 404


def test_bind_pins_message_id_when_unset(client: TestClient, db):
    user = make_user(db, username="bind-pin")
    conv = _make_conv(db, user)
    msg = Message(conversation_id=conv.id, role="user", content="知道這是啥嗎")
    db.add(msg)
    db.commit()
    db.refresh(msg)
    att = _row(db, user=user, conv=conv)

    resp = client.post(
        "/api/attachments/bind",
        headers=_auth(client, "bind-pin"),
        json={
            "conversation_id": conv.id,
            "reference_ids": [att.reference_id],
            "message_id": msg.id,
        },
    )
    assert resp.status_code == 200, resp.text
    db.refresh(att)
    assert att.message_id == msg.id
    assert resp.json()["attachments"][0]["message_id"] == msg.id


def test_bind_does_not_steal_existing_message_id(client: TestClient, db):
    user = make_user(db, username="bind-keep-msg")
    conv = _make_conv(db, user)
    first = Message(conversation_id=conv.id, role="user", content="first")
    second = Message(conversation_id=conv.id, role="user", content="second")
    db.add_all([first, second])
    db.commit()
    db.refresh(first)
    db.refresh(second)
    att = _row(db, user=user, conv=conv)
    att.message_id = first.id
    db.commit()

    resp = client.post(
        "/api/attachments/bind",
        headers=_auth(client, "bind-keep-msg"),
        json={
            "conversation_id": conv.id,
            "reference_ids": [att.reference_id],
            "message_id": second.id,
        },
    )
    assert resp.status_code == 200, resp.text
    db.refresh(att)
    assert att.message_id == first.id


def test_bind_rejects_message_from_other_conversation(client: TestClient, db):
    user = make_user(db, username="bind-msg-miss")
    conv = _make_conv(db, user)
    other = _make_conv(db, user, title="other")
    msg = Message(conversation_id=other.id, role="user", content="nope")
    db.add(msg)
    db.commit()
    db.refresh(msg)
    att = _row(db, user=user)

    resp = client.post(
        "/api/attachments/bind",
        headers=_auth(client, "bind-msg-miss"),
        json={
            "conversation_id": conv.id,
            "reference_ids": [att.reference_id],
            "message_id": msg.id,
        },
    )
    assert resp.status_code == 404
    db.refresh(att)
    assert att.conversation_id is None
    assert att.message_id is None
