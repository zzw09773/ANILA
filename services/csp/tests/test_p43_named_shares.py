# -*- coding: utf-8 -*-
"""P4.3 — named share to people/units; anonymous link retired.

Invariant-proving tests. Each acceptance case names the production
mutation that turns it red (see class docstrings / comments).
"""
from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.conversation import Conversation, ConversationShare
from app.models.department import Department
from app.models.user import User
from app.utils.security import create_access_token
from tests.conftest import make_user


def _bearer(user) -> dict:
    token = create_access_token({
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "tv": user.token_version,
    })
    return {"Authorization": f"Bearer {token}"}


def _make_dept(db: Session, name: str, parent_id: int | None = None) -> Department:
    d = Department(name=name, parent_id=parent_id, is_active=True)
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


def _make_conv(db: Session, user: User, level: str = "無機密") -> Conversation:
    conv = Conversation(user_id=user.id, title=f"p43-{level}")
    db.add(conv)
    db.commit()
    conv.classification_level = level
    from app.schemas.contracts.classification import ClassificationLevel as CL
    conv.classified = CL.from_storage(level) >= CL.RESTRICTED
    db.commit()
    db.refresh(conv)
    return conv


# ── Inv 1: person XOR unit; dept reaches descendants; re-parent at read ─────


class TestInvariant1NamedTargetsAndSubtree:
    """Mutant: hard-code scope to [target_department_id] only (no descendants)
    → grandchild read fails. Mutant: freeze membership at share time
    → re-parent after share is not honoured.
    """

    def test_department_share_reaches_grandchild(
        self, client: TestClient, db: Session
    ):
        owner = make_user(db, username="p43_owner_g")
        root = _make_dept(db, "p43-院-g")
        mid = _make_dept(db, "p43-所-g", parent_id=root.id)
        leaf = _make_dept(db, "p43-組-g", parent_id=mid.id)
        grandchild = make_user(db, username="p43_gc", department_id=leaf.id)
        outsider = make_user(db, username="p43_out", department_id=None)

        conv = _make_conv(db, owner, "無機密")
        resp = client.post(
            f"/api/conversations/{conv.id}/shares",
            headers=_bearer(owner),
            json={"target_department_id": root.id},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["target_department_id"] == root.id
        assert body.get("token") is None

        ok = client.get(
            f"/api/conversations/{conv.id}",
            headers=_bearer(grandchild),
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["id"] == conv.id

        denied = client.get(
            f"/api/conversations/{conv.id}",
            headers=_bearer(outsider),
        )
        assert denied.status_code == 403

    def test_reparent_between_share_and_read_honoured(
        self, client: TestClient, db: Session
    ):
        owner = make_user(db, username="p43_owner_rp")
        a = _make_dept(db, "p43-A-rp")
        b = _make_dept(db, "p43-B-rp")
        child = _make_dept(db, "p43-child-rp", parent_id=b.id)
        reader = make_user(db, username="p43_reader_rp", department_id=child.id)

        conv = _make_conv(db, owner, "無機密")
        share = client.post(
            f"/api/conversations/{conv.id}/shares",
            headers=_bearer(owner),
            json={"target_department_id": a.id},
        )
        assert share.status_code == 201, share.text

        # Before re-parent: child under B → not in A's scope.
        before = client.get(
            f"/api/conversations/{conv.id}",
            headers=_bearer(reader),
        )
        assert before.status_code == 403

        # Re-parent child under A; read-time expansion must grant access.
        child.parent_id = a.id
        db.commit()

        after = client.get(
            f"/api/conversations/{conv.id}",
            headers=_bearer(reader),
        )
        assert after.status_code == 200, after.text

    def test_person_share_by_username(self, client: TestClient, db: Session):
        owner = make_user(db, username="p43_owner_p")
        peer = make_user(db, username="p43_peer_p")
        conv = _make_conv(db, owner, "無機密")
        resp = client.post(
            f"/api/conversations/{conv.id}/shares",
            headers=_bearer(owner),
            json={"target_username": "p43_peer_p"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["target_user_id"] == peer.id
        assert resp.json()["target_username"] == "p43_peer_p"

        listed = client.get("/api/conversations", headers=_bearer(peer))
        assert listed.status_code == 200
        assert any(c["id"] == conv.id for c in listed.json())


# ── Inv 2: access checked every read; revoke blocks unread ──────────────────


class TestInvariant2RevokeBlocksUnread:
    """Mutant: skip user_has_active_share in _check_read_access
    → revoke still allows GET. Mutant: check share only at create time
    and cache a grant flag on the conversation → revoke ignored.
    """

    def test_revoke_blocks_not_yet_opened(self, client: TestClient, db: Session):
        owner = make_user(db, username="p43_owner_rev")
        peer = make_user(db, username="p43_peer_rev")
        conv = _make_conv(db, owner, "無機密")
        created = client.post(
            f"/api/conversations/{conv.id}/shares",
            headers=_bearer(owner),
            json={"target_username": peer.username},
        )
        assert created.status_code == 201, created.text
        share_id = created.json()["id"]

        # Recipient has not opened yet; revoke.
        rev = client.delete(
            f"/api/conversations/{conv.id}/shares/{share_id}",
            headers=_bearer(owner),
        )
        assert rev.status_code == 204, rev.text

        blocked = client.get(
            f"/api/conversations/{conv.id}",
            headers=_bearer(peer),
        )
        assert blocked.status_code == 403
        assert db.query(ConversationShare).filter(
            ConversationShare.id == share_id
        ).count() == 0


# ── Inv 3: classification threshold + audit ─────────────────────────────────


class TestInvariant3ClassificationThreshold:
    """Mutant: restore old detail「列管對話不允許建立分享連結」without naming
    the level → assertion on detail fails. Mutant: allow 密 → 201.
    """

    def test_refuse_restricted_names_level(self, client: TestClient, db: Session):
        owner = make_user(db, username="p43_owner_cls")
        peer = make_user(db, username="p43_peer_cls")
        conv = _make_conv(db, owner, "密")
        resp = client.post(
            f"/api/conversations/{conv.id}/shares",
            headers=_bearer(owner),
            json={"target_username": peer.username},
        )
        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert "密" in detail
        assert "營業秘密" in detail
        assert db.query(ConversationShare).filter(
            ConversationShare.conversation_id == conv.id
        ).count() == 0

    def test_trade_secret_allows_and_audits(self, client: TestClient, db: Session):
        owner = make_user(db, username="p43_owner_ts")
        peer = make_user(db, username="p43_peer_ts")
        conv = _make_conv(db, owner, "營業秘密")
        before = db.query(AuditLog).filter(
            AuditLog.action == "share_conversation",
            AuditLog.resource_id == str(conv.id),
        ).count()
        resp = client.post(
            f"/api/conversations/{conv.id}/shares",
            headers=_bearer(owner),
            json={"target_username": peer.username},
        )
        assert resp.status_code == 201, resp.text
        after = db.query(AuditLog).filter(
            AuditLog.action == "share_conversation",
            AuditLog.resource_id == str(conv.id),
        ).count()
        assert after - before == 1


# ── Inv 4: anonymous link removed ───────────────────────────────────────────


class TestInvariant4AnonymousLinkGone:
    """Mutant: restore public_share router + ENABLE_PUBLIC_SHARE → this
    404 becomes 200.
    """

    def test_public_share_route_gone(self, client: TestClient, db: Session):
        resp = client.get("/api/public/share/any-token-at-all")
        assert resp.status_code == 404

    def test_create_without_target_refuses(self, client: TestClient, db: Session):
        owner = make_user(db, username="p43_owner_anon")
        conv = _make_conv(db, owner, "無機密")
        resp = client.post(
            f"/api/conversations/{conv.id}/shares",
            headers=_bearer(owner),
            json={"mode": "read_only"},
        )
        assert resp.status_code == 400
        assert "匿名" in resp.json()["detail"] or "指定" in resp.json()["detail"]


# ── Inv 5: fail closed for unnameable targets ───────────────────────────────


class TestInvariant5FailClosedUnnameable:
    """Mutant: skip is_active check → disabled user share succeeds.
    Mutant: missing dept returns empty share instead of 404.
    """

    def test_missing_user(self, client: TestClient, db: Session):
        owner = make_user(db, username="p43_owner_mu")
        conv = _make_conv(db, owner, "無機密")
        resp = client.post(
            f"/api/conversations/{conv.id}/shares",
            headers=_bearer(owner),
            json={"target_username": "no-such-user-xyz"},
        )
        assert resp.status_code == 404
        assert "找不到帳號" in resp.json()["detail"]

    def test_disabled_user(self, client: TestClient, db: Session):
        owner = make_user(db, username="p43_owner_du")
        peer = make_user(db, username="p43_peer_du")
        peer.is_active = False
        db.commit()
        conv = _make_conv(db, owner, "無機密")
        resp = client.post(
            f"/api/conversations/{conv.id}/shares",
            headers=_bearer(owner),
            json={"target_username": peer.username},
        )
        assert resp.status_code == 400
        assert "停用" in resp.json()["detail"]

    def test_missing_department(self, client: TestClient, db: Session):
        owner = make_user(db, username="p43_owner_md")
        conv = _make_conv(db, owner, "無機密")
        resp = client.post(
            f"/api/conversations/{conv.id}/shares",
            headers=_bearer(owner),
            json={"target_department_name": "不存在的單位-xyz"},
        )
        assert resp.status_code == 404
        assert "找不到單位" in resp.json()["detail"]

    def test_inactive_department(self, client: TestClient, db: Session):
        owner = make_user(db, username="p43_owner_id")
        dept = _make_dept(db, "p43-inactive-dept")
        dept.is_active = False
        db.commit()
        conv = _make_conv(db, owner, "無機密")
        resp = client.post(
            f"/api/conversations/{conv.id}/shares",
            headers=_bearer(owner),
            json={"target_department_id": dept.id},
        )
        assert resp.status_code == 400
        assert "停用" in resp.json()["detail"]


# ── Review follow-ups (expires_at / classify-after-share / revoke path) ──────


class TestReviewFollowups:
    def test_expires_at_naive_future_still_readable(
        self, client: TestClient, db: Session
    ):
        """HIGH: naive PG-style expires_at must not TypeError on read."""
        from datetime import datetime, timedelta, timezone

        from app.services.conversation_service import _as_utc

        owner = make_user(db, username="p43_owner_exp")
        peer = make_user(db, username="p43_peer_exp")
        conv = _make_conv(db, owner, "無機密")
        future = datetime.now(timezone.utc) + timedelta(hours=24)
        created = client.post(
            f"/api/conversations/{conv.id}/shares",
            headers=_bearer(owner),
            json={
                "target_username": peer.username,
                "expires_at": future.isoformat(),
            },
        )
        assert created.status_code == 201, created.text
        share = db.query(ConversationShare).filter(
            ConversationShare.id == created.json()["id"]
        ).one()
        # Simulate PG TIMESTAMP WITHOUT TIME ZONE round-trip.
        share.expires_at = _as_utc(share.expires_at).replace(tzinfo=None)
        db.commit()

        ok = client.get(
            f"/api/conversations/{conv.id}",
            headers=_bearer(peer),
        )
        assert ok.status_code == 200, ok.text

    def test_expires_at_naive_past_blocks_read(
        self, client: TestClient, db: Session
    ):
        from datetime import datetime, timedelta, timezone

        owner = make_user(db, username="p43_owner_exp2")
        peer = make_user(db, username="p43_peer_exp2")
        conv = _make_conv(db, owner, "無機密")
        created = client.post(
            f"/api/conversations/{conv.id}/shares",
            headers=_bearer(owner),
            json={"target_username": peer.username},
        )
        assert created.status_code == 201, created.text
        share = db.query(ConversationShare).filter(
            ConversationShare.id == created.json()["id"]
        ).one()
        share.expires_at = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).replace(tzinfo=None)
        db.commit()

        blocked = client.get(
            f"/api/conversations/{conv.id}",
            headers=_bearer(peer),
        )
        assert blocked.status_code == 403

    def test_classify_after_share_blocks_recipient(
        self, client: TestClient, db: Session
    ):
        owner = make_user(db, username="p43_owner_latch")
        peer = make_user(db, username="p43_peer_latch")
        conv = _make_conv(db, owner, "無機密")
        created = client.post(
            f"/api/conversations/{conv.id}/shares",
            headers=_bearer(owner),
            json={"target_username": peer.username},
        )
        assert created.status_code == 201, created.text
        assert client.get(
            f"/api/conversations/{conv.id}", headers=_bearer(peer),
        ).status_code == 200

        assert client.post(
            f"/api/conversations/{conv.id}/classify",
            headers=_bearer(owner),
        ).status_code == 200

        blocked = client.get(
            f"/api/conversations/{conv.id}",
            headers=_bearer(peer),
        )
        assert blocked.status_code == 403
        assert "密" in blocked.json()["detail"]

    def test_revoke_wrong_conv_id_is_404(self, client: TestClient, db: Session):
        owner = make_user(db, username="p43_owner_idor")
        peer = make_user(db, username="p43_peer_idor")
        conv_a = _make_conv(db, owner, "無機密")
        conv_b = _make_conv(db, owner, "無機密")
        created = client.post(
            f"/api/conversations/{conv_a.id}/shares",
            headers=_bearer(owner),
            json={"target_username": peer.username},
        )
        assert created.status_code == 201, created.text
        share_id = created.json()["id"]
        resp = client.delete(
            f"/api/conversations/{conv_b.id}/shares/{share_id}",
            headers=_bearer(owner),
        )
        assert resp.status_code == 404
        assert db.query(ConversationShare).filter(
            ConversationShare.id == share_id
        ).count() == 1

    def test_recipient_view_all_forced_to_active(
        self, client: TestClient, db: Session
    ):
        from app.models.message import Message

        owner = make_user(db, username="p43_owner_view")
        peer = make_user(db, username="p43_peer_view")
        conv = _make_conv(db, owner, "無機密")
        m1 = Message(conversation_id=conv.id, role="user", content="active-Q")
        db.add(m1)
        db.flush()
        m2 = Message(
            conversation_id=conv.id, role="assistant", content="abandoned",
            parent_id=m1.id,
        )
        m3 = Message(
            conversation_id=conv.id, role="assistant", content="active-A",
            parent_id=m1.id,
        )
        db.add_all([m2, m3])
        db.flush()
        conv.active_leaf_message_id = m3.id
        db.commit()

        assert client.post(
            f"/api/conversations/{conv.id}/shares",
            headers=_bearer(owner),
            json={"target_username": peer.username},
        ).status_code == 201

        detail = client.get(
            f"/api/conversations/{conv.id}?view=all",
            headers=_bearer(peer),
        )
        assert detail.status_code == 200, detail.text
        bodies = [m["content"] for m in detail.json()["messages"]]
        assert "abandoned" not in bodies
        assert bodies == ["active-Q", "active-A"]
