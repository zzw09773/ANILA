# -*- coding: utf-8 -*-
"""OE-4 — SYSTEM-MAP §8 L241-242 two-line thresholds.

Truth table per action face:
  無機密     → allow, no audit
  營業秘密   → allow + audit row
  密         → deny
  機密       → deny

Covers: share, conversation read-audit, artifact export, G4 task-less
ceiling PolicyDecision, public share, search snippet.
"""
from __future__ import annotations

import json
import os
import secrets

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.models.artifact import ExportRecord
from app.models.audit_log import AuditLog
from app.models.conversation import Conversation, ConversationShare
from app.models.message import Message
from app.models.policy_decision import PolicyDecision
from app.models.task import Task
from app.schemas.contracts.classification import (
    ClassificationLevel,
    classification_audit_required,
    outbound_action_allowed,
)
from types import SimpleNamespace

from app.services.proxy.ceiling import enforce_model_ceiling
from app.services.proxy.task_link import TaskRunContext
from app.utils.security import create_access_token
from tests.conftest import make_model, make_user

LEVELS = ["無機密", "營業秘密", "密", "機密"]
SVC_TOKEN = "svc-oe4-test-token"
_SVC = {"X-CSP-Service-Token": SVC_TOKEN}
MARKER = "oe4snippetmarker"


def _caller(user):
    return SimpleNamespace(user=user, api_key_id=None)


def _model_decisions(db, model_id):
    return (
        db.query(PolicyDecision)
        .filter(
            PolicyDecision.action == "model.invoke",
            PolicyDecision.resource_id == str(model_id),
        )
        .all()
    )


@pytest.fixture(autouse=True)
def _svc_token(monkeypatch):
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", SVC_TOKEN)
    yield


def _bearer(user) -> dict:
    token = create_access_token({
        "sub": str(user.id), "username": user.username,
        "role": user.role, "tv": user.token_version,
    })
    return {"Authorization": f"Bearer {token}"}


def _make_conv(db: Session, user, level: str) -> Conversation:
    conv = Conversation(user_id=user.id, title=f"oe4-{level}")
    db.add(conv)
    db.commit()
    conv.classification_level = level
    # Mirror display flag the way apply_classification would (level >= 密).
    from app.schemas.contracts.classification import ClassificationLevel as CL
    conv.classified = CL.from_storage(level) >= CL.RESTRICTED
    db.commit()
    db.refresh(conv)
    return conv


def _add_message(db: Session, conv: Conversation, content: str) -> Message:
    msg = Message(conversation_id=conv.id, role="user", content=content)
    db.add(msg)
    db.flush()
    # OW-1: public-share / active-path reads walk active_leaf_message_id.
    conv.active_leaf_message_id = msg.id
    db.commit()
    db.refresh(msg)
    return msg


def _make_task(db: Session, user, level: str = "無機密") -> Task:
    task = Task(
        title="oe4", task_type="query", requester_user_id=user.id,
        status="submitted", classification_level=level,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _register_artifact(client: TestClient, *, task_id: int) -> dict:
    resp = client.post(
        "/v1/artifacts",
        headers=_SVC,
        json={
            "artifact_type": "report",
            "title": "oe4",
            "storage_ref": "store://oe4/a.pdf",
            "task_id": task_id,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── Helpers ───────────────────────────────────────────────────────────────────


class TestTwoLineHelpers:
    def test_outbound_and_audit_truth_table(self):
        expected = {
            "無機密": (True, False),
            "營業秘密": (True, True),
            "密": (False, True),
            "機密": (False, True),
        }
        for value, (allow, audit) in expected.items():
            level = ClassificationLevel.from_storage(value)
            assert outbound_action_allowed(level) is allow, value
            assert classification_audit_required(level) is audit, value


# ── Share ─────────────────────────────────────────────────────────────────────


class TestShareTwoLine:
    @pytest.mark.parametrize("level", LEVELS)
    def test_share_truth_table(self, client: TestClient, db: Session, level: str):
        user = make_user(db, username=f"share_{level}")
        peer = make_user(db, username=f"share_peer_{level}")
        conv = _make_conv(db, user, level)
        before = db.query(AuditLog).filter(
            AuditLog.action == "share_conversation",
            AuditLog.resource_id == str(conv.id),
        ).count()
        resp = client.post(
            f"/api/conversations/{conv.id}/shares",
            headers=_bearer(user),
            json={"mode": "read_only", "target_username": peer.username},
        )
        allow = outbound_action_allowed(ClassificationLevel.from_storage(level))
        audit = classification_audit_required(ClassificationLevel.from_storage(level))
        if allow:
            assert resp.status_code == 201, resp.text
            assert db.query(ConversationShare).filter(
                ConversationShare.conversation_id == conv.id
            ).count() == 1
            rows = (
                db.query(AuditLog)
                .filter(
                    AuditLog.action == "share_conversation",
                    AuditLog.resource_id == str(conv.id),
                )
                .order_by(AuditLog.id)
                .all()
            )
            assert len(rows) - before == (1 if audit else 0)
            if audit:
                row = rows[-1]
                assert row.actor_user_id == user.id
                assert row.actor_username == user.username
                meta = json.loads(row.metadata_json or "{}")
                assert meta.get("classification_level") == level
        else:
            assert resp.status_code == 403
            assert db.query(ConversationShare).filter(
                ConversationShare.conversation_id == conv.id
            ).count() == 0

    def test_share_follows_level_not_classified_boolean(
        self, client: TestClient, db: Session
    ):
        """Mutation-killer: classified=True disagreeing with level must not
        block share; gate follows LEVEL (營業秘密 → allow + audit)."""
        user = make_user(db, username="share_mut_killer")
        peer = make_user(db, username="share_mut_peer")
        conv = Conversation(user_id=user.id, title="oe4-mut")
        db.add(conv)
        db.commit()
        conv.classification_level = "營業秘密"
        conv.classified = True  # deliberate disagreement with level mirror
        db.commit()
        db.refresh(conv)
        assert conv.classified is True
        assert conv.classification_level == "營業秘密"

        before = db.query(AuditLog).filter(
            AuditLog.action == "share_conversation",
            AuditLog.resource_id == str(conv.id),
        ).count()
        resp = client.post(
            f"/api/conversations/{conv.id}/shares",
            headers=_bearer(user),
            json={"mode": "read_only", "target_username": peer.username},
        )
        assert resp.status_code == 201, resp.text
        assert db.query(ConversationShare).filter(
            ConversationShare.conversation_id == conv.id
        ).count() == 1
        rows = (
            db.query(AuditLog)
            .filter(
                AuditLog.action == "share_conversation",
                AuditLog.resource_id == str(conv.id),
            )
            .order_by(AuditLog.id)
            .all()
        )
        assert len(rows) - before == 1
        row = rows[-1]
        assert row.actor_user_id == user.id
        assert row.actor_username == user.username
        meta = json.loads(row.metadata_json or "{}")
        assert meta.get("classification_level") == "營業秘密"


# ── Conversation read-audit ───────────────────────────────────────────────────


class TestReadAuditTwoLine:
    @pytest.mark.parametrize("level", LEVELS)
    def test_get_conversation_read_audit(
        self, client: TestClient, db: Session, level: str
    ):
        user = make_user(db, username=f"read_{level}")
        conv = _make_conv(db, user, level)
        before = db.query(AuditLog).filter(
            AuditLog.action == "access_classified_conversation",
            AuditLog.resource_id == str(conv.id),
        ).count()
        resp = client.get(
            f"/api/conversations/{conv.id}", headers=_bearer(user),
        )
        assert resp.status_code == 200, resp.text
        after = db.query(AuditLog).filter(
            AuditLog.action == "access_classified_conversation",
            AuditLog.resource_id == str(conv.id),
        ).count()
        expect_audit = classification_audit_required(
            ClassificationLevel.from_storage(level)
        )
        assert after - before == (1 if expect_audit else 0)


# ── Search snippet ────────────────────────────────────────────────────────────


class TestSearchSnippetTwoLine:
    @pytest.mark.parametrize("level", LEVELS)
    def test_search_snippet_truth_table(
        self, client: TestClient, db: Session, level: str
    ):
        user = make_user(db, username=f"srch_{level}")
        conv = _make_conv(db, user, level)
        _add_message(db, conv, f"hello {MARKER} world at {level}")
        # Second matching message in same conv — audit must still be one row.
        _add_message(db, conv, f"again {MARKER} twin")

        before = db.query(AuditLog).filter(
            AuditLog.action == "access_classified_conversation",
            AuditLog.resource_id == str(conv.id),
        ).count()
        resp = client.get(
            f"/api/conversations/search?q={MARKER}",
            headers=_bearer(user),
        )
        assert resp.status_code == 200, resp.text
        hits = [h for h in resp.json() if h["id"] == conv.id]
        assert len(hits) == 1
        hit = hits[0]
        allow = outbound_action_allowed(ClassificationLevel.from_storage(level))
        expect_audit = (
            classification_audit_required(ClassificationLevel.from_storage(level))
            and allow
        )
        if allow:
            assert hit["snippet"] is not None
            assert MARKER in hit["snippet"]
        else:
            assert hit["snippet"] is None
        after = db.query(AuditLog).filter(
            AuditLog.action == "access_classified_conversation",
            AuditLog.resource_id == str(conv.id),
        ).count()
        assert after - before == (1 if expect_audit else 0)
        if expect_audit:
            row = (
                db.query(AuditLog)
                .filter(
                    AuditLog.action == "access_classified_conversation",
                    AuditLog.resource_id == str(conv.id),
                )
                .order_by(AuditLog.id.desc())
                .first()
            )
            assert row.actor_user_id == user.id
            assert row.actor_username == user.username

    def test_title_only_trade_secret_no_snippet_no_audit(
        self, client: TestClient, db: Session
    ):
        """營業秘密 matched only by title → snippet None, zero audit rows."""
        title_marker = f"oe4titleonly_{secrets.token_hex(4)}"
        user = make_user(db, username="srch_title_only")
        conv = _make_conv(db, user, "營業秘密")
        conv.title = title_marker
        db.commit()
        db.refresh(conv)
        _add_message(db, conv, "no marker in message body at all")

        before = db.query(AuditLog).filter(
            AuditLog.action == "access_classified_conversation",
            AuditLog.resource_id == str(conv.id),
        ).count()
        resp = client.get(
            f"/api/conversations/search?q={title_marker}",
            headers=_bearer(user),
        )
        assert resp.status_code == 200, resp.text
        hits = [h for h in resp.json() if h["id"] == conv.id]
        assert len(hits) == 1
        assert hits[0]["snippet"] is None
        after = db.query(AuditLog).filter(
            AuditLog.action == "access_classified_conversation",
            AuditLog.resource_id == str(conv.id),
        ).count()
        assert after - before == 0


# ── Artifact export ───────────────────────────────────────────────────────────


class TestArtifactExportTwoLine:
    @pytest.mark.parametrize("level", LEVELS)
    def test_export_truth_table(self, client: TestClient, db: Session, level: str):
        user = make_user(db, username=f"exp_{level}")
        task = _make_task(db, user, level=level)
        art_id = _register_artifact(client, task_id=task.id)["artifact_id"]
        # target_classification_floor is optional metadata-only (OE-4).
        resp = client.post(
            f"/v1/artifacts/{art_id}/exports",
            headers=_SVC,
            json={},
        )
        allow = outbound_action_allowed(ClassificationLevel.from_storage(level))
        if allow:
            assert resp.status_code == 201, resp.text
            assert db.query(ExportRecord).filter(
                ExportRecord.artifact_id == art_id
            ).count() == 1
            pd = db.query(PolicyDecision).filter(
                PolicyDecision.action == "artifact.export",
                PolicyDecision.resource_id == str(art_id),
            ).one()
            assert pd.decision == "allow"
        else:
            assert resp.status_code == 403
            assert db.query(ExportRecord).filter(
                ExportRecord.artifact_id == art_id
            ).count() == 0
            pd = db.query(PolicyDecision).filter(
                PolicyDecision.action == "artifact.export",
                PolicyDecision.resource_id == str(art_id),
            ).one()
            assert pd.decision == "deny"


# ── G4 task-less ceiling PolicyDecision ───────────────────────────────────────


class TestG4TasklessPolicyRecord:
    def test_unclassified_taskless_no_row(self, db: Session):
        user = make_user(db, "g4_unc")
        m = make_model(db, name="g4_unc_m")
        m.classification_ceiling = "機密"
        db.commit()
        enforce_model_ceiling(
            db, model=m, caller=_caller(user), task_ctx=None, conv_id_int=None,
        )
        assert _model_decisions(db, m.id) == []

    @pytest.mark.parametrize("level", ["營業秘密", "密", "機密"])
    def test_audited_levels_taskless_allow_writes_row(
        self, db: Session, level: str
    ):
        """≥ 營業秘密 task-less allow writes the same PolicyDecision shape."""
        user = make_user(db, f"g4_{level}")
        m = make_model(db, name=f"g4_m_{level}")
        # Ceiling high enough that the evaluated level still passes.
        m.classification_ceiling = "機密"
        db.commit()
        conv = Conversation(user_id=user.id, title="g4")
        db.add(conv)
        db.commit()
        conv.classification_level = level
        db.commit()
        enforce_model_ceiling(
            db, model=m, caller=_caller(user), task_ctx=None, conv_id_int=conv.id,
        )
        allows = [d for d in _model_decisions(db, m.id) if d.decision == "allow"]
        assert len(allows) == 1
        assert allows[0].task_id is None

    def test_task_linked_still_records(self, db: Session):
        user = make_user(db, "g4_task")
        m = make_model(db, name="g4_task_m")
        m.classification_ceiling = "機密"
        db.commit()
        task = Task(title="t", task_type="query", requester_user_id=user.id)
        db.add(task)
        db.commit()
        ctx = TaskRunContext(task_id=task.id, trace_id=task.trace_id, task_run_id=1)
        enforce_model_ceiling(
            db, model=m, caller=_caller(user), task_ctx=ctx, conv_id_int=None,
        )
        allows = [d for d in _model_decisions(db, m.id) if d.decision == "allow"]
        assert len(allows) == 1
        assert allows[0].task_id == task.id
