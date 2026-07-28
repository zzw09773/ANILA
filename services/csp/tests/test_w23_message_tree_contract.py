# -*- coding: utf-8 -*-
"""W2-3 contract + SQLite-runnable edit semantics (no throwaway PG required)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from app.models.audit_log import AuditLog
from app.models.conversation import Conversation
from app.models.message import Message
from app.services import conversation_service as svc
from tests.conftest import login, make_user


# MessageOut keys for default (non-tree) read — pin the pre-W2-3 contract.
_MESSAGE_OUT_KEYS = {
    "id",
    "role",
    "content",
    "trace_id",
    "latency_ms",
    "model_name",
    "agent_name",
    "metadata",
    "rating",
    "created_at",
    "attachments",
}


def _auth(client, username: str) -> dict:
    return {"Authorization": f"Bearer {login(client, username=username)}"}


def test_default_read_message_shape_excludes_parent_id(client, db):
    """A5: default GET detail messages keep the old-client key set (no parent_id)."""
    make_user(db, username="w23-shape")
    headers = _auth(client, "w23-shape")

    created = client.post(
        "/api/conversations",
        headers=headers,
        json={"title": "shape", "origin": "anila-ui"},
    )
    assert created.status_code == 201, created.text
    conv_id = created.json()["id"]

    for role, content in (("user", "u1"), ("assistant", "a1")):
        r = client.post(
            f"/api/conversations/{conv_id}/messages",
            headers=headers,
            json={"role": role, "content": content},
        )
        assert r.status_code == 201, r.text

    detail = client.get(f"/api/conversations/{conv_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert "messages" in body
    assert len(body["messages"]) == 2
    for msg in body["messages"]:
        assert set(msg.keys()) == _MESSAGE_OUT_KEYS
        assert "parent_id" not in msg

    tree = client.get(f"/api/conversations/{conv_id}?tree=1", headers=headers)
    assert tree.status_code == 200, tree.text
    tbody = tree.json()
    assert "active_leaf_message_id" in tbody
    assert len(tbody["messages"]) == 2
    for msg in tbody["messages"]:
        assert "parent_id" in msg


def test_edit_grows_sibling_no_delete_sqlite(db):
    """Edit path creates a sibling; truncation delete is gone."""
    user = make_user(db, username="w23-edit")
    conv = Conversation(user_id=user.id, title="t")
    db.add(conv)
    db.commit()
    db.refresh(conv)

    ids = []
    parent = None
    for role, text_body in [
        ("user", "u0"),
        ("assistant", "a0"),
        ("user", "u1"),
        ("assistant", "a1"),
        ("user", "u2"),
    ]:
        m = Message(
            conversation_id=conv.id,
            parent_id=parent,
            role=role,
            content=text_body,
        )
        db.add(m)
        db.flush()
        ids.append(m.id)
        parent = m.id
    conv.active_leaf_message_id = ids[-1]
    db.commit()
    db.refresh(conv)

    sibling = svc.edit_user_message(db, conv.id, ids[2], user, "u1-edited")
    db.refresh(conv)

    all_msgs = db.query(Message).filter(Message.conversation_id == conv.id).all()
    assert len(all_msgs) == 6
    assert sibling.id not in ids
    assert sibling.parent_id == db.get(Message, ids[2]).parent_id
    for mid in ids:
        assert db.get(Message, mid) is not None
    assert conv.active_leaf_message_id == sibling.id

    audits = db.query(AuditLog).filter(AuditLog.action == "edit_user_message").all()
    assert any(a.resource_id == str(sibling.id) for a in audits)


def test_legal_hold_rejects_edit_sqlite(db):
    user = make_user(db, username="w23-hold")
    conv = Conversation(user_id=user.id, title="held", legal_hold=True)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    m = Message(conversation_id=conv.id, role="user", content="x")
    db.add(m)
    db.commit()
    db.refresh(m)

    with pytest.raises(HTTPException) as ei:
        svc.edit_user_message(db, conv.id, m.id, user, "y")
    assert ei.value.status_code == 403


def test_edit_path_has_no_synchronize_session_false_delete():
    """A5: grep guard — truncation delete must be absent from the service."""
    src = Path(__file__).resolve().parents[1] / "app/services/conversation_service.py"
    text = src.read_text(encoding="utf-8")
    assert ".delete(synchronize_session=False)" not in text


def test_fork_assistant_creates_sibling(db):
    user = make_user(db, username="w23-fork")
    conv = Conversation(user_id=user.id, title="f")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    u = Message(conversation_id=conv.id, role="user", content="q")
    db.add(u)
    db.flush()
    a = Message(
        conversation_id=conv.id, parent_id=u.id, role="assistant", content="a1"
    )
    db.add(a)
    db.flush()
    conv.active_leaf_message_id = a.id
    db.commit()
    db.refresh(a)

    sibling = svc.fork_assistant_message(
        db, conv.id, a.id, user, content="a2"
    )
    db.refresh(conv)
    assert sibling.id != a.id
    assert sibling.parent_id == a.parent_id == u.id
    assert conv.active_leaf_message_id == sibling.id
    assert db.query(Message).filter(Message.conversation_id == conv.id).count() == 3


def test_reaper_guard_whole_tree_legal_hold(db):
    user = make_user(db, username="w23-reap")
    conv = Conversation(user_id=user.id, title="r", legal_hold=True)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    m = Message(conversation_id=conv.id, role="user", content="x")
    db.add(m)
    db.commit()
    db.refresh(m)
    assert svc.conversation_is_under_legal_hold(conv) is True
    assert svc.reaper_may_consider_branch_message(db, conv, m) is False


_CLASSIFICATION_LEVELS = (
    "無機密",
    "營業秘密",
    "機密",
    "極機密",
    "絕對機密",
)


@pytest.mark.parametrize("level", _CLASSIFICATION_LEVELS)
@pytest.mark.parametrize("endpoint", ("edit", "fork"))
def test_edit_fork_access_owner_ok_non_owner_403(client, db, level, endpoint):
    """Access rule: owner succeeds, non-owner 403 — independent of classification_level."""
    tag = abs(hash(f"{level}-{endpoint}")) % 10_000_000
    owner = make_user(db, username=f"w23o{tag}")
    other = make_user(db, username=f"w23n{tag}")
    owner_h = _auth(client, owner.username)
    other_h = _auth(client, other.username)

    created = client.post(
        "/api/conversations",
        headers=owner_h,
        json={"title": "access", "origin": "anila-ui"},
    )
    assert created.status_code == 201, created.text
    conv_id = created.json()["id"]

    conv = db.get(Conversation, conv_id)
    assert conv is not None
    conv.classification_level = level
    db.commit()

    u = client.post(
        f"/api/conversations/{conv_id}/messages",
        headers=owner_h,
        json={"role": "user", "content": "q"},
    )
    assert u.status_code == 201, u.text
    uid = u.json()["id"]
    a = client.post(
        f"/api/conversations/{conv_id}/messages",
        headers=owner_h,
        json={"role": "assistant", "content": "a"},
    )
    assert a.status_code == 201, a.text
    aid = a.json()["id"]

    if endpoint == "edit":
        path = f"/api/conversations/{conv_id}/messages/{uid}/edit"
        owner_resp = client.put(path, headers=owner_h, json={"content": "q2"})
        other_resp = client.put(path, headers=other_h, json={"content": "stolen"})
    else:
        path = f"/api/conversations/{conv_id}/messages/{aid}/fork"
        owner_resp = client.post(path, headers=owner_h, json={"content": "a2"})
        other_resp = client.post(path, headers=other_h, json={"content": "stolen"})

    assert owner_resp.status_code in (200, 201), owner_resp.text
    assert other_resp.status_code == 403, other_resp.text


@pytest.mark.parametrize("endpoint", ("edit", "fork"))
def test_edit_fork_admin_tier_not_owner_allowed(client, db, endpoint):
    """Admin tier edits/forks someone else's conversation —— **by design**, pinned.

    ``_check_access`` returns early for ``is_admin_tier`` (admin + owner), so an
    admin who is not the owner gets 200/201 on both endpoints, unlike the plain
    non-owner who gets 403 above. That is the documented guard behaviour, not a
    hole —— but it was previously unrecorded, so a future tightening (or
    accidental widening) of the admin scope would slip through silently. This
    test exists so that change shows up as a failing assertion.
    """
    tag = abs(hash(f"admin-{endpoint}")) % 10_000_000
    owner = make_user(db, username=f"w23ao{tag}")
    admin = make_user(db, username=f"w23aa{tag}", role="admin")
    owner_h = _auth(client, owner.username)
    admin_h = _auth(client, admin.username)

    created = client.post(
        "/api/conversations",
        headers=owner_h,
        json={"title": "admin-access", "origin": "anila-ui"},
    )
    assert created.status_code == 201, created.text
    conv_id = created.json()["id"]

    u = client.post(
        f"/api/conversations/{conv_id}/messages",
        headers=owner_h,
        json={"role": "user", "content": "q"},
    )
    assert u.status_code == 201, u.text
    uid = u.json()["id"]
    a = client.post(
        f"/api/conversations/{conv_id}/messages",
        headers=owner_h,
        json={"role": "assistant", "content": "a"},
    )
    assert a.status_code == 201, a.text
    aid = a.json()["id"]

    if endpoint == "edit":
        resp = client.put(
            f"/api/conversations/{conv_id}/messages/{uid}/edit",
            headers=admin_h,
            json={"content": "admin-edit"},
        )
    else:
        resp = client.post(
            f"/api/conversations/{conv_id}/messages/{aid}/fork",
            headers=admin_h,
            json={"content": "admin-fork"},
        )

    assert resp.status_code in (200, 201), resp.text
    # The sibling really landed in the owner's conversation.
    grown = db.query(Message).filter(Message.conversation_id == conv_id).count()
    assert grown == 3


def test_active_leaf_rejected_under_legal_hold(client, db):
    user = make_user(db, username="w23-leaf-hold")
    headers = _auth(client, user.username)
    created = client.post(
        "/api/conversations",
        headers=headers,
        json={"title": "held-leaf", "origin": "anila-ui"},
    )
    assert created.status_code == 201, created.text
    conv_id = created.json()["id"]
    msg = client.post(
        f"/api/conversations/{conv_id}/messages",
        headers=headers,
        json={"role": "user", "content": "x"},
    )
    assert msg.status_code == 201, msg.text
    mid = msg.json()["id"]

    conv = db.get(Conversation, conv_id)
    conv.legal_hold = True
    db.commit()

    resp = client.put(
        f"/api/conversations/{conv_id}/active-leaf",
        headers=headers,
        json={"message_id": mid},
    )
    assert resp.status_code == 403, resp.text
    assert "法律保全" in resp.text


# ── W2-3 補遺:誰宣告父節點 ───────────────────────────────────────────────────
#
# `append_message` 的 `parent_id=None` 語意 = 「接到 active leaf」。那對「使用者
# 送出新的一輪」是對的,對任何「我要長一個兄弟」的呼叫端都是錯的 —— active leaf
# 是伺服器端狀態,別的動作會把它搬走。線路層因此必須讓呼叫端**說得出**位置。


def _seed_linear_conv(client, headers) -> tuple[int, int, int]:
    """conv + user#1 + assistant#2(active leaf = 2)。回傳 (conv_id, u1, a1)。"""
    created = client.post(
        "/api/conversations",
        headers=headers,
        json={"title": "parent-decl", "origin": "anila-ui"},
    )
    assert created.status_code == 201, created.text
    conv_id = created.json()["id"]
    ids = []
    for role, content in (("user", "u1"), ("assistant", "a1")):
        r = client.post(
            f"/api/conversations/{conv_id}/messages",
            headers=headers,
            json={"role": role, "content": content},
        )
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
    return conv_id, ids[0], ids[1]


def test_append_with_explicit_parent_lands_as_sibling_not_under_active_leaf(client, db):
    """指名 parent_id → 與 active leaf 無關,長成兄弟(reviewer 探針的線路層對應)。"""
    make_user(db, username="w23-parent-explicit")
    headers = _auth(client, "w23-parent-explicit")
    conv_id, u1, a1 = _seed_linear_conv(client, headers)

    # active leaf 此刻是 a1;隱式 append 會掛在它底下。指名 u1 才是兄弟。
    r = client.post(
        f"/api/conversations/{conv_id}/messages",
        headers=headers,
        json={"role": "assistant", "content": "recovered", "parent_id": u1},
    )
    assert r.status_code == 201, r.text
    recovered = r.json()["id"]

    tree = client.get(f"/api/conversations/{conv_id}?tree=1", headers=headers).json()
    by_id = {m["id"]: m for m in tree["messages"]}
    assert by_id[recovered]["parent_id"] == u1
    assert by_id[recovered]["parent_id"] == by_id[a1]["parent_id"]
    assert by_id[recovered]["parent_id"] != a1
    # 新列仍然成為 active leaf(既有語意不變)。
    assert tree["active_leaf_message_id"] == recovered


def test_append_without_parent_still_extends_active_leaf(client, db):
    """向後相容:沒送 parent_id 的舊 client 行為原封不動(這是刻意不加必填的理由)。"""
    make_user(db, username="w23-parent-implicit")
    headers = _auth(client, "w23-parent-implicit")
    conv_id, _u1, a1 = _seed_linear_conv(client, headers)

    r = client.post(
        f"/api/conversations/{conv_id}/messages",
        headers=headers,
        json={"role": "user", "content": "next turn"},
    )
    assert r.status_code == 201, r.text
    tree = client.get(f"/api/conversations/{conv_id}?tree=1", headers=headers).json()
    by_id = {m["id"]: m for m in tree["messages"]}
    assert by_id[r.json()["id"]]["parent_id"] == a1


def test_append_rejects_parent_from_another_conversation(client, db):
    """指名的父節點必須屬於同一個對話 —— 不能拿別人的樹當錨點。"""
    make_user(db, username="w23-parent-crossconv")
    headers = _auth(client, "w23-parent-crossconv")
    conv_a, u1_a, _ = _seed_linear_conv(client, headers)
    conv_b, _, _ = _seed_linear_conv(client, headers)
    assert conv_a != conv_b

    r = client.post(
        f"/api/conversations/{conv_b}/messages",
        headers=headers,
        json={"role": "assistant", "content": "x", "parent_id": u1_a},
    )
    assert r.status_code == 400, r.text
    assert "parent_id" in r.text


def test_append_message_docstring_states_when_implicit_is_correct():
    """預設值的 docstring 必須說明「什麼時候依賴它才是對的」,不是只描述機制。"""
    doc = svc.append_message.__doc__ or ""
    assert "active leaf" in doc
    assert "brand-new user turn" in doc
    assert "sibling" in doc
