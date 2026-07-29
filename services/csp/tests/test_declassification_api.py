# -*- coding: utf-8 -*-
"""Slice 3b — 降級申請 / 權責指派 API + 四級 runtime latch 測試。

依 docs/anila-redesign-docs/08(§7 變體 A 雙人原則/權責脫鉤/信任錨/
fail-closed、§8 DeclassificationRequest、§12 supervisor_missing)與 doc 09
§11(路由形狀:declassification-requests 建立/列表/approve/reject)。

涵蓋:
- 降級申請:僅 Admin 可建立(ADR-0005)、非 Admin 403、非降級 422
- 裁決:in_system happy path、紙本代錄有/無文號、申請人 ≠ 核准人 403、
  無權責 fail-closed 維持 pending + 403、駁回不動等級 + PolicyDecision 記帳
- 權責指派:owner-only 授予、必附核定依據、雙人控制(登錄 → 另一人確認才
  生效)、登錄人不得自我確認、撤銷 soft 後失效、未確認的授予不生效
- runtime 四級 latch:agent latch 寫等級 + event + boolean 鏡射、
  _agent_policy_level backfill、manual classify 走單向核心、conversation
  payload 帶 classification_level、conversation→task 傳遞
"""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from app.api.proxy import (
    _agent_policy_level,
    _latch_agent_classification,
    _propagate_conversation_level_to_task,
)
from app.models.audit_log import AuditLog
from app.models.classification import (
    ClassificationAuthorityAssignment,
    ClassificationEvent,
    DeclassificationRequest,
)
from app.models.conversation import Conversation
from app.models.policy_decision import PolicyDecision
from app.models.task import Task
from app.modules.policy import (
    apply_classification,
    has_declassification_authority,
)
from app.schemas.contracts.classification import ClassificationLevel
from app.services import conversation_service
from tests.conftest import login, make_user


# ── helpers ────────────────────────────────────────────────────────────────────


def _headers(client, username: str) -> dict:
    return {"Authorization": f"Bearer {login(client, username)}"}


def _make_conversation(db, user, level: str | None = None) -> Conversation:
    conv = Conversation(user_id=user.id, title="測試對話")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    if level is not None:
        apply_classification(
            db,
            resource_type="conversation",
            resource_id=str(conv.id),
            new_level=level,
            actor_type="user",
            actor_id=str(user.id),
            reason="manual_admin",
        )
        db.refresh(conv)
    return conv


def _grant_effective_authority(db, user, reference="總字第0001號簽呈"):
    """直接落一筆已確認且生效的權責(繞開 API 雙人流程,隔離裁決測試)。"""
    row = ClassificationAuthorityAssignment(
        user_id=user.id,
        authority_reference=reference,
        confirmed_by_user_id=user.id,
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _create_request(client, headers, conv, to_level="無機密") -> dict:
    resp = client.post(
        "/api/classification/declassification-requests",
        headers=headers,
        json={
            "resource_type": "conversation",
            "resource_id": str(conv.id),
            "requested_level": to_level,
            "reason": "專案結案,已完成降密審查",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── 降級申請 API ────────────────────────────────────────────────────────────────


class TestDeclassificationRequestApi:
    def test_admin_creates_and_lists_with_filter(self, client, db):
        admin = make_user(db, "boss", role="admin")
        conv = _make_conversation(db, admin, level="機密")
        headers = _headers(client, "boss")
        created = _create_request(client, headers, conv)
        assert created["status"] == "pending_supervisor"
        assert created["from_level"] == "機密"
        assert created["to_level"] == "無機密"
        assert created["requested_by_admin_id"] == admin.id

        listed = client.get(
            "/api/classification/declassification-requests",
            headers=headers,
            params={"status": "pending_supervisor"},
        )
        assert listed.status_code == 200, listed.text
        assert [r["id"] for r in listed.json()] == [created["id"]]
        # 過濾到別的狀態 → 空
        empty = client.get(
            "/api/classification/declassification-requests",
            headers=headers,
            params={"status": "applied"},
        )
        assert empty.json() == []

    def test_non_admin_cannot_create_403(self, client, db):
        admin = make_user(db, "boss", role="admin")
        make_user(db, "mallory", role="user")
        conv = _make_conversation(db, admin, level="機密")
        resp = client.post(
            "/api/classification/declassification-requests",
            headers=_headers(client, "mallory"),
            json={
                "resource_type": "conversation",
                "resource_id": str(conv.id),
                "requested_level": "無機密",
                "reason": "not allowed",
            },
        )
        assert resp.status_code == 403
        assert db.query(DeclassificationRequest).count() == 0

    def test_not_a_downgrade_422(self, client, db):
        admin = make_user(db, "boss", role="admin")
        conv = _make_conversation(db, admin, level="密")
        resp = client.post(
            "/api/classification/declassification-requests",
            headers=_headers(client, "boss"),
            json={
                "resource_type": "conversation",
                "resource_id": str(conv.id),
                "requested_level": "機密",  # 嚴格更高 → 非降級
                "reason": "wrong direction",
            },
        )
        assert resp.status_code == 422

    def test_requester_equals_approver_403(self, client, db):
        admin = make_user(db, "boss", role="admin")
        _grant_effective_authority(db, admin)  # 即便有權責,仍不得自我核准
        conv = _make_conversation(db, admin, level="機密")
        headers = _headers(client, "boss")
        created = _create_request(client, headers, conv)
        resp = client.post(
            f"/api/classification/declassification-requests/{created['id']}/approve",
            headers=headers,
            json={"via": "in_system"},
        )
        assert resp.status_code == 403
        db.refresh(conv)
        assert conv.classification_level == "機密"

    def test_approve_in_system_happy_path_lowers_and_records_decision(
        self, client, db
    ):
        admin = make_user(db, "boss", role="admin")
        supervisor = make_user(db, "sup", role="admin")
        _grant_effective_authority(db, supervisor)
        conv = _make_conversation(db, admin, level="機密")
        created = _create_request(client, _headers(client, "boss"), conv)

        resp = client.post(
            f"/api/classification/declassification-requests/{created['id']}/approve",
            headers=_headers(client, "sup"),
            json={"via": "in_system", "comment": "同意降級"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "applied"
        assert body["approved_via"] == "in_system"
        assert body["supervisor_user_id"] == supervisor.id
        db.refresh(conv)
        assert conv.classification_level == "無機密"
        assert conv.classified is False  # 鏡射:降到無機密 → boolean 回 false
        # PolicyDecision 記帳(action=classification.downgrade_request、allow)
        decision = (
            db.query(PolicyDecision)
            .filter(PolicyDecision.action == "classification.downgrade_request")
            .one()
        )
        assert decision.decision == "allow"
        assert decision.actor_id == supervisor.id

    def test_approve_paper_without_docno_422(self, client, db):
        admin = make_user(db, "boss", role="admin")
        recorder = make_user(db, "rec", role="admin")
        _grant_effective_authority(db, recorder)
        conv = _make_conversation(db, admin, level="機密")
        created = _create_request(client, _headers(client, "boss"), conv)
        headers = _headers(client, "rec")
        # 缺文號
        r1 = client.post(
            f"/api/classification/declassification-requests/{created['id']}/approve",
            headers=headers,
            json={"via": "recorded_paper_decision",
                  "authority_title_name": "院長 王大明"},
        )
        assert r1.status_code == 422
        # 缺官職姓名
        r2 = client.post(
            f"/api/classification/declassification-requests/{created['id']}/approve",
            headers=headers,
            json={"via": "recorded_paper_decision",
                  "authority_reference": "院授字第1130001號"},
        )
        assert r2.status_code == 422
        db.refresh(conv)
        assert conv.classification_level == "機密"  # 仍未降

    def test_approve_paper_with_docno_records_authority(self, client, db):
        admin = make_user(db, "boss", role="admin")
        recorder = make_user(db, "rec", role="admin")
        _grant_effective_authority(db, recorder)
        conv = _make_conversation(db, admin, level="機密")
        created = _create_request(client, _headers(client, "boss"), conv)
        resp = client.post(
            f"/api/classification/declassification-requests/{created['id']}/approve",
            headers=_headers(client, "rec"),
            json={
                "via": "recorded_paper_decision",
                "authority_reference": "院授字第1130001號",
                "authority_title_name": "院長 王大明",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "applied"
        assert body["authority_reference"] == "院授字第1130001號"
        assert body["authority_title_name"] == "院長 王大明"
        assert body["recorded_by_user_id"] == recorder.id

    def test_no_authority_fail_closed_stays_pending_403(self, client, db):
        admin = make_user(db, "boss", role="admin")
        make_user(db, "powerless", role="owner")  # owner 但無權責指派
        conv = _make_conversation(db, admin, level="機密")
        created = _create_request(client, _headers(client, "boss"), conv)
        resp = client.post(
            f"/api/classification/declassification-requests/{created['id']}/approve",
            headers=_headers(client, "powerless"),
            json={"via": "in_system"},
        )
        assert resp.status_code == 403
        # 申請維持 pending、資源未降、supervisor_missing 已 audit
        req = db.get(DeclassificationRequest, created["id"])
        assert req.status == "pending_supervisor"
        db.refresh(conv)
        assert conv.classification_level == "機密"
        assert (
            db.query(AuditLog)
            .filter(AuditLog.action == "supervisor_missing")
            .count()
            == 1
        )

    def test_reject_keeps_level_and_records_deny(self, client, db):
        admin = make_user(db, "boss", role="admin")
        supervisor = make_user(db, "sup", role="admin")
        _grant_effective_authority(db, supervisor)
        conv = _make_conversation(db, admin, level="機密")
        created = _create_request(client, _headers(client, "boss"), conv)
        resp = client.post(
            f"/api/classification/declassification-requests/{created['id']}/reject",
            headers=_headers(client, "sup"),
            json={"reason": "降密審查未通過"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "rejected"
        db.refresh(conv)
        assert conv.classification_level == "機密"
        decision = (
            db.query(PolicyDecision)
            .filter(PolicyDecision.action == "classification.downgrade_request")
            .one()
        )
        assert decision.decision == "deny"


# ── 「機密審批權責」指派 API(雙人控制、脫鉤、信任錨)────────────────────────────


class TestClassificationAuthorityApi:
    def test_grant_requires_owner(self, client, db):
        make_user(db, "adm", role="admin")
        target = make_user(db, "holder", role="admin")
        resp = client.post(
            "/api/classification-authorities",
            headers=_headers(client, "adm"),
            json={"user_id": target.id, "authority_reference": "簽呈-1"},
        )
        assert resp.status_code == 403  # admin ≠ owner

    def test_grant_requires_reference_422(self, client, db):
        make_user(db, "boss", role="owner")
        target = make_user(db, "holder", role="admin")
        resp = client.post(
            "/api/classification-authorities",
            headers=_headers(client, "boss"),
            json={"user_id": target.id, "authority_reference": "   "},
        )
        assert resp.status_code == 422

    def test_grant_creates_pending_not_effective(self, client, db):
        make_user(db, "boss", role="owner")
        target = make_user(db, "holder", role="admin")
        resp = client.post(
            "/api/classification-authorities",
            headers=_headers(client, "boss"),
            json={"user_id": target.id, "authority_reference": "院授字第0002號"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["is_active"] is False
        assert body["is_effective"] is False
        assert body["confirmed_by_user_id"] is None
        # 未確認 → hook 視為未生效(fail-closed)
        assert has_declassification_authority(db, target.id) is False

    def test_confirm_dual_person_makes_effective(self, client, db):
        make_user(db, "boss", role="owner")
        confirmer = make_user(db, "second", role="admin")
        target = make_user(db, "holder", role="admin")
        created = client.post(
            "/api/classification-authorities",
            headers=_headers(client, "boss"),
            json={"user_id": target.id, "authority_reference": "院授字第0003號"},
        ).json()

        resp = client.post(
            f"/api/classification-authorities/{created['id']}/confirm",
            headers=_headers(client, "second"),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["is_active"] is True
        assert body["is_effective"] is True
        assert body["confirmed_by_user_id"] == confirmer.id
        assert has_declassification_authority(db, target.id) is True

    def test_confirm_by_granter_forbidden(self, client, db):
        make_user(db, "boss", role="owner")
        target = make_user(db, "holder", role="admin")
        created = client.post(
            "/api/classification-authorities",
            headers=_headers(client, "boss"),
            json={"user_id": target.id, "authority_reference": "院授字第0004號"},
        ).json()
        # 登錄人(owner boss)不得確認自己登錄的指派(雙人控制)
        resp = client.post(
            f"/api/classification-authorities/{created['id']}/confirm",
            headers=_headers(client, "boss"),
        )
        assert resp.status_code == 403
        assert has_declassification_authority(db, target.id) is False

    def test_revoke_soft_makes_not_effective(self, client, db):
        make_user(db, "boss", role="owner")
        make_user(db, "second", role="admin")
        target = make_user(db, "holder", role="admin")
        created = client.post(
            "/api/classification-authorities",
            headers=_headers(client, "boss"),
            json={"user_id": target.id, "authority_reference": "院授字第0005號"},
        ).json()
        client.post(
            f"/api/classification-authorities/{created['id']}/confirm",
            headers=_headers(client, "second"),
        )
        assert has_declassification_authority(db, target.id) is True

        resp = client.delete(
            f"/api/classification-authorities/{created['id']}",
            headers=_headers(client, "boss"),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["is_active"] is False
        assert body["is_effective"] is False
        assert body["revoked_at"] is not None
        assert has_declassification_authority(db, target.id) is False

    def test_unconfirmed_grant_not_effective_for_decide(self, client, db):
        admin = make_user(db, "boss", role="owner")  # owner 兼申請人
        supervisor = make_user(db, "sup", role="admin")
        conv = _make_conversation(db, admin, level="機密")
        created = _create_request(client, _headers(client, "boss"), conv)
        # 授予但未確認 → 不生效(pending confirm)
        grant = client.post(
            "/api/classification-authorities",
            headers=_headers(client, "boss"),
            json={"user_id": supervisor.id, "authority_reference": "院授字第0006號"},
        )
        assert grant.status_code == 201
        assert grant.json()["is_effective"] is False
        # supervisor 仍無生效權責 → 裁決 fail-closed 403,申請維持 pending
        resp = client.post(
            f"/api/classification/declassification-requests/{created['id']}/approve",
            headers=_headers(client, "sup"),
            json={"via": "in_system"},
        )
        assert resp.status_code == 403
        assert (
            db.get(DeclassificationRequest, created["id"]).status
            == "pending_supervisor"
        )


# ── runtime 四級 latch(proxy helpers + manual classify + payload + task)──────────


class TestRuntimeFourLevelLatch:
    def test_agent_latch_writes_level_event_and_boolean_mirror(self, db):
        user = make_user(db)
        conv = _make_conversation(db, user)
        _latch_agent_classification(db, conv.id, "機密")
        db.refresh(conv)
        assert conv.classification_level == "機密"
        assert conv.classified is True  # 鏡射
        assert conv.classification_inherited is False  # agent 來源非繼承
        event = (
            db.query(ClassificationEvent)
            .filter(ClassificationEvent.resource_type == "conversation")
            .one()
        )
        assert event.reason == "agent_policy"
        assert event.new_level == "機密"

    def test_agent_policy_level_backfill_and_default(self):
        # requires_encryption 但未設等級 → floor RESTRICTED(密);舊 rank-2
        enc = SimpleNamespace(
            default_classification_level="無機密", requires_encryption=True
        )
        assert _agent_policy_level(enc) is ClassificationLevel.RESTRICTED
        # 已設更高等級 → 取更高(max)
        top = SimpleNamespace(
            default_classification_level="機密", requires_encryption=True
        )
        assert _agent_policy_level(top) is ClassificationLevel.SECRET
        # 無加密、無等級 → 無機密(不 latch)
        plain = SimpleNamespace(
            default_classification_level="無機密", requires_encryption=False
        )
        assert _agent_policy_level(plain) is ClassificationLevel.UNCLASSIFIED

    def test_manual_classify_routes_through_core(self, client, db):
        user = make_user(db, "owner-u", role="user")
        conv = _make_conversation(db, user)
        result = conversation_service.classify_conversation(db, conv.id, user)
        assert result.classification_level == "密"
        assert result.classified is True
        assert result.classified_by == user.id
        event = (
            db.query(ClassificationEvent)
            .filter(ClassificationEvent.reason == "manual_admin")
            .one()
        )
        assert event.new_level == "密"
        # 既有 AuditLog 相容軌跡保留
        assert (
            db.query(AuditLog)
            .filter(AuditLog.action == "classify_conversation")
            .count()
            == 1
        )

    def test_conversation_payload_has_classification_level(self, client, db):
        user = make_user(db, "alice")
        plain = _make_conversation(db, user)
        classified = _make_conversation(db, user, level="機密")
        headers = _headers(client, "alice")
        r_plain = client.get(
            f"/api/conversations/{plain.id}", headers=headers
        )
        assert r_plain.status_code == 200, r_plain.text
        assert r_plain.json()["classification_level"] == "無機密"
        r_hi = client.get(
            f"/api/conversations/{classified.id}", headers=headers
        )
        assert r_hi.json()["classification_level"] == "機密"

    def test_task_propagation_carries_conversation_level(self, db):
        user = make_user(db)
        conv = _make_conversation(db, user, level="機密")
        task = Task(title="t", task_type="query", requester_user_id=user.id)
        db.add(task)
        db.commit()
        db.refresh(task)
        assert task.classification_level == "無機密"

        _propagate_conversation_level_to_task(db, task.id, conv.id)
        db.refresh(task)
        assert task.classification_level == "機密"
        event = (
            db.query(ClassificationEvent)
            .filter(ClassificationEvent.resource_type == "task")
            .one()
        )
        assert event.reason == "source_selected"
        assert event.new_level == "機密"

    def test_task_propagation_noop_when_unclassified(self, db):
        user = make_user(db)
        conv = _make_conversation(db, user)  # 無機密
        task = Task(title="t", task_type="query", requester_user_id=user.id)
        db.add(task)
        db.commit()
        db.refresh(task)
        _propagate_conversation_level_to_task(db, task.id, conv.id)
        db.refresh(task)
        assert task.classification_level == "無機密"
        assert (
            db.query(ClassificationEvent)
            .filter(ClassificationEvent.resource_type == "task")
            .count()
            == 0
        )
