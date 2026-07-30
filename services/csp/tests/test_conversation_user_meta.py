# -*- coding: utf-8 -*-
"""Conversation star / folder / tags survive reload (per-user meta).

Invariants + mutant notes are in each test docstring.
"""
from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.conversation import Conversation, ConversationUserMeta
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


def _make_conv(db: Session, user: User, *, classified: bool = False) -> Conversation:
    conv = Conversation(
        user_id=user.id,
        title="meta-conv",
        classified=classified,
        classification_level="密" if classified else "無機密",
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


class TestStarFolderTagPersist:
    """Inv 1 — star/folder/tags survive list reload.

    Mutant: map defaults only (skip upsert / skip enrich) → list returns
    starred=false / folder=all / tags=[] after PUT.
    """

    def test_star_folder_tags_survive_list_reload(
        self, client: TestClient, db: Session
    ):
        user = make_user(db, username="meta_owner")
        conv = _make_conv(db, user)
        h = _bearer(user)

        put = client.put(
            f"/api/conversations/{conv.id}",
            headers=h,
            json={
                "starred": True,
                "folder": "usr-project-x",
                "tags": ["urgent", "hr"],
            },
        )
        assert put.status_code == 200, put.text
        body = put.json()
        assert body["starred"] is True
        assert body["folder"] == "usr-project-x"
        assert body["tags"] == ["urgent", "hr"]

        listed = client.get("/api/conversations", headers=h)
        assert listed.status_code == 200
        row = next(r for r in listed.json() if r["id"] == conv.id)
        assert row["starred"] is True
        assert row["folder"] == "usr-project-x"
        assert row["tags"] == ["urgent", "hr"]

        # Meta is per-user, not on the Conversation row.
        assert db.query(ConversationUserMeta).filter_by(
            user_id=user.id, conversation_id=conv.id,
        ).count() == 1


class TestClassifiedTagDerived:
    """Inv 2 — classified tag is derived; user cannot add or clear it.

    Mutant A: stop stripping ``classified`` on write → user_tags stores it
    and removing it from a later PUT clears the derived tag on a classified
    conversation.
    Mutant B: stop appending classified on read → classified conv list
    omits the tag.
    """

    def test_cannot_add_or_clear_classified_tag(
        self, client: TestClient, db: Session
    ):
        user = make_user(db, username="meta_cls")
        conv = _make_conv(db, user, classified=True)
        h = _bearer(user)

        # Trying to "add" only classified → stored empty; read still shows it.
        put = client.put(
            f"/api/conversations/{conv.id}",
            headers=h,
            json={"tags": ["classified"]},
        )
        assert put.status_code == 200, put.text
        assert put.json()["tags"] == ["classified"]
        meta = db.query(ConversationUserMeta).filter_by(
            user_id=user.id, conversation_id=conv.id,
        ).one()
        assert "classified" not in (meta.user_tags or [])

        # User tags persist alongside the derived tag.
        put2 = client.put(
            f"/api/conversations/{conv.id}",
            headers=h,
            json={"tags": ["urgent", "classified"]},
        )
        assert put2.status_code == 200
        assert put2.json()["tags"] == ["urgent", "classified"]

        # Clearing user tags (and "removing" classified) cannot drop derived.
        put3 = client.put(
            f"/api/conversations/{conv.id}",
            headers=h,
            json={"tags": []},
        )
        assert put3.status_code == 200
        assert put3.json()["tags"] == ["classified"]

        listed = client.get("/api/conversations", headers=h).json()
        row = next(r for r in listed if r["id"] == conv.id)
        assert row["tags"] == ["classified"]


class TestShareIsolation:
    """Shared-conversation answer: A's meta is invisible to B.

    Mutant: store meta on Conversation columns / return owner's meta for
    every caller → B's list shows A's star.
    """

    def test_share_recipient_does_not_see_owner_meta(
        self, client: TestClient, db: Session
    ):
        owner = make_user(db, username="meta_share_o")
        peer = make_user(db, username="meta_share_p")
        conv = _make_conv(db, owner)
        oh, ph = _bearer(owner), _bearer(peer)

        assert client.put(
            f"/api/conversations/{conv.id}",
            headers=oh,
            json={"starred": True, "folder": "usr-mine", "tags": ["owner-only"]},
        ).status_code == 200

        share = client.post(
            f"/api/conversations/{conv.id}/shares",
            headers=oh,
            json={"target_user_id": peer.id},
        )
        assert share.status_code == 201, share.text

        peer_list = client.get("/api/conversations", headers=ph)
        assert peer_list.status_code == 200
        row = next(r for r in peer_list.json() if r["id"] == conv.id)
        assert row["starred"] is False
        assert row["folder"] == "all"
        assert row["tags"] == []

        # Peer can set their own meta without colliding.
        assert client.put(
            f"/api/conversations/{conv.id}",
            headers=ph,
            json={"starred": True, "folder": "usr-peer", "tags": ["peer-tag"]},
        ).status_code == 200

        owner_row = next(
            r for r in client.get("/api/conversations", headers=oh).json()
            if r["id"] == conv.id
        )
        assert owner_row["folder"] == "usr-mine"
        assert owner_row["tags"] == ["owner-only"]
        peer_row = next(
            r for r in client.get("/api/conversations", headers=ph).json()
            if r["id"] == conv.id
        )
        assert peer_row["folder"] == "usr-peer"
        assert peer_row["tags"] == ["peer-tag"]


class TestSaveFailureDoesNotLie:
    """Inv 3 — API must reject empty patch; client rollback is covered in shell.

    Mutant: accept ``{}`` as success → UI would keep optimistic state with
    nothing persisted.
    """

    def test_empty_patch_rejected(self, client: TestClient, db: Session):
        user = make_user(db, username="meta_empty")
        conv = _make_conv(db, user)
        resp = client.put(
            f"/api/conversations/{conv.id}",
            headers=_bearer(user),
            json={},
        )
        assert resp.status_code == 400


class TestDeleteCleansMeta:
    """SQLite has no FK CASCADE — delete_conversation must bulk-delete meta.

    Mutant: skip ConversationUserMeta delete in delete_conversation → orphans=1.
    """

    def test_delete_conversation_removes_user_meta(
        self, client: TestClient, db: Session
    ):
        user = make_user(db, username="meta_del")
        conv = _make_conv(db, user)
        h = _bearer(user)
        assert client.put(
            f"/api/conversations/{conv.id}",
            headers=h,
            json={"starred": True, "tags": ["x"]},
        ).status_code == 200
        assert db.query(ConversationUserMeta).filter_by(
            conversation_id=conv.id,
        ).count() == 1
        assert client.delete(
            f"/api/conversations/{conv.id}", headers=h,
        ).status_code == 204
        assert db.query(ConversationUserMeta).filter_by(
            conversation_id=conv.id,
        ).count() == 0
