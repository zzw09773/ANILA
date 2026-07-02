# -*- coding: utf-8 -*-
"""Slice 3a — 五級分類 schema 升級 + latch core 測試。

依 doc 08(§1 五級排序、§2 單向閂鎖、§3 backfill bridge、§6
ClassificationEvent 7 值 reason enum、§7 變體 A 雙人原則/權責脫鉤/
fail-closed、§8 DeclassificationRequest 5 值 status + approved_via 二選一、
§12 supervisor_missing fail-closed)與 doc 10 Slice 3(舊 boolean latch 不破)。

涵蓋:
- doc 08 enum 逐字驗證(reason 7 值、status 5 值、approved_via 2 值)
- apply_classification 單向閂鎖:升級寫 event、降級嘗試 no-op 不寫 event
  (doc 08 未規定降級嘗試要記 event → 依指示採「無 event、回 None」)
- max 傳遞、legacy boolean 鏡射(classified = level >= 機密)雙向一致
- 降級申請:僅 Admin 可申請、申請人 ≠ 核准人、無權責 fail-closed 停留
  pending + audit supervisor_missing、紙本代錄必附文號/官職姓名、
  核准恰好降一次 + event
- 未知 resource_type / reason / level fail-closed ValueError
- migration chain:單一 head = r1_0004(Slice 5a 後)
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from app.models.audit_log import AuditLog
from app.models.classification import (
    ClassificationAuthorityAssignment,
    ClassificationEvent,
    DeclassificationRequest,
)
from app.models.conversation import Conversation
from app.models.task import Task
from app.modules.policy import (
    apply_classification,
    create_declassification_request,
    decide_declassification,
    effective_level,
    has_declassification_authority,
)
from app.schemas.contracts.classification import (
    ClassificationEventReason,
    ClassificationLevel,
    DeclassificationApprovedVia,
    DeclassificationStatus,
)
from tests.conftest import make_user


# ── 佈景 helper ────────────────────────────────────────────────────────────────


def make_conversation(db, user) -> Conversation:
    conv = Conversation(user_id=user.id, title="測試對話")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def grant_authority(db, user, reference="總字第0001號簽呈"):
    row = ClassificationAuthorityAssignment(
        user_id=user.id,
        authority_reference=reference,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def latch_confidential(db, conv, actor) -> ClassificationEvent:
    return apply_classification(
        db,
        resource_type="conversation",
        resource_id=str(conv.id),
        new_level="機密",
        actor_type="user",
        actor_id=str(actor.id),
        reason="manual_admin",
    )


# ── doc 08 enum 逐字驗證 ──────────────────────────────────────────────────────


class TestDoc08EnumsVerbatim:
    def test_event_reason_seven_values_in_doc_order(self):
        assert [r.value for r in ClassificationEventReason] == [
            "source_selected",
            "agent_policy",
            "memory_inherited",
            "manual_admin",
            "service_policy",
            "content_detection",
            "declassification_copy",
        ]

    def test_declassification_status_five_values_in_doc_order(self):
        assert [s.value for s in DeclassificationStatus] == [
            "pending_supervisor",
            "approved",
            "rejected",
            "cancelled",
            "applied",
        ]

    def test_approved_via_two_values(self):
        assert [v.value for v in DeclassificationApprovedVia] == [
            "in_system",
            "recorded_paper_decision",
        ]

    def test_backfill_mapping_from_legacy_boolean(self):
        # doc 08 §3 migration bridge:false → 無機密、true → 機密(floor)
        assert (
            ClassificationLevel.from_legacy_classified(False)
            is ClassificationLevel.UNCLASSIFIED
        )
        assert (
            ClassificationLevel.from_legacy_classified(True)
            is ClassificationLevel.CONFIDENTIAL
        )


# ── apply_classification 單向閂鎖 ─────────────────────────────────────────────


class TestApplyClassification:
    def test_raise_writes_event_and_mirrors_legacy_boolean(self, db):
        user = make_user(db)
        conv = make_conversation(db, user)
        assert conv.classified is False

        event = latch_confidential(db, conv, user)

        assert isinstance(event, ClassificationEvent)
        assert event.resource_type == "conversation"
        assert event.resource_id == str(conv.id)
        assert event.previous_level == "無機密"
        assert event.new_level == "機密"
        assert event.reason == "manual_admin"
        assert event.actor_user_id == user.id
        db.refresh(conv)
        assert conv.classification_level == "機密"
        assert conv.classification_latched_at is not None
        assert conv.classification_source == "propagation"
        assert conv.classification_event_id == event.id
        # 鏡射:classified = level >= 機密(舊 latch 不破)
        assert conv.classified is True
        assert conv.classified_at is not None

    def test_lowering_attempt_is_noop_without_event(self, db):
        user = make_user(db)
        conv = make_conversation(db, user)
        latch_confidential(db, conv, user)

        result = apply_classification(
            db,
            resource_type="conversation",
            resource_id=str(conv.id),
            new_level="無機密",
            actor_type="user",
            actor_id=str(user.id),
            reason="manual_admin",
        )

        assert result is None
        db.refresh(conv)
        assert conv.classification_level == "機密"
        assert conv.classified is True
        assert db.query(ClassificationEvent).count() == 1

    def test_equal_level_is_noop(self, db):
        user = make_user(db)
        conv = make_conversation(db, user)
        latch_confidential(db, conv, user)
        assert latch_confidential(db, conv, user) is None
        assert db.query(ClassificationEvent).count() == 1

    def test_max_propagation_keeps_raising(self, db):
        user = make_user(db)
        conv = make_conversation(db, user)
        latch_confidential(db, conv, user)

        event = apply_classification(
            db,
            resource_type="conversation",
            resource_id=str(conv.id),
            new_level="極機密",
            actor_type="service",
            actor_id="router",
            reason="content_detection",
            source="content_detection",
        )

        assert event.previous_level == "機密"
        assert event.new_level == "極機密"
        assert event.actor_user_id is None  # service actor 非 users FK
        db.refresh(conv)
        assert conv.classification_level == "極機密"
        assert conv.classification_source == "content_detection"
        assert conv.classified is True

    def test_below_confidential_does_not_flip_legacy_boolean(self, db):
        user = make_user(db)
        conv = make_conversation(db, user)
        event = apply_classification(
            db,
            resource_type="conversation",
            resource_id=str(conv.id),
            new_level="營業秘密",
            actor_type="user",
            actor_id=str(user.id),
            reason="source_selected",
        )
        assert event.new_level == "營業秘密"
        db.refresh(conv)
        assert conv.classification_level == "營業秘密"
        # 鏡射一致:營業秘密 < 機密 → 舊 boolean 維持 false
        assert conv.classified is False

    def test_memory_inherited_reason_mirrors_inherited_flag(self, db):
        user = make_user(db)
        conv = make_conversation(db, user)
        apply_classification(
            db,
            resource_type="conversation",
            resource_id=str(conv.id),
            new_level="機密",
            actor_type="service",
            actor_id="memory",
            reason="memory_inherited",
        )
        db.refresh(conv)
        assert conv.classified is True
        assert conv.classification_inherited is True

    def test_task_id_resolves_trace_id_onto_event(self, db):
        user = make_user(db)
        conv = make_conversation(db, user)
        task = Task(title="t", task_type="query", requester_user_id=user.id)
        db.add(task)
        db.commit()
        event = apply_classification(
            db,
            resource_type="conversation",
            resource_id=str(conv.id),
            new_level="機密",
            actor_type="user",
            actor_id=str(user.id),
            reason="agent_policy",
            task_id=task.id,
        )
        assert event.trace_id == task.trace_id

    def test_unknown_resource_type_fail_closed(self, db):
        user = make_user(db)
        with pytest.raises(ValueError):
            apply_classification(
                db,
                resource_type="starship",
                resource_id="1",
                new_level="機密",
                actor_type="user",
                actor_id=str(user.id),
                reason="manual_admin",
            )
        assert db.query(ClassificationEvent).count() == 0

    def test_unknown_reason_fail_closed(self, db):
        user = make_user(db)
        conv = make_conversation(db, user)
        with pytest.raises(ValueError):
            apply_classification(
                db,
                resource_type="conversation",
                resource_id=str(conv.id),
                new_level="機密",
                actor_type="user",
                actor_id=str(user.id),
                reason="just_because",
            )
        assert db.query(ClassificationEvent).count() == 0

    def test_unknown_level_fail_closed(self, db):
        user = make_user(db)
        conv = make_conversation(db, user)
        with pytest.raises(ValueError):
            apply_classification(
                db,
                resource_type="conversation",
                resource_id=str(conv.id),
                new_level="公開",
                actor_type="user",
                actor_id=str(user.id),
                reason="manual_admin",
            )

    def test_missing_resource_fail_closed(self, db):
        user = make_user(db)
        with pytest.raises(ValueError):
            apply_classification(
                db,
                resource_type="conversation",
                resource_id="424242",
                actor_type="user",
                actor_id=str(user.id),
                new_level="機密",
                reason="manual_admin",
            )

    def test_effective_level_roundtrip(self, db):
        user = make_user(db)
        conv = make_conversation(db, user)
        assert effective_level(
            db, resource_type="conversation", resource_id=str(conv.id)
        ) is ClassificationLevel.UNCLASSIFIED
        latch_confidential(db, conv, user)
        assert effective_level(
            db, resource_type="conversation", resource_id=str(conv.id)
        ) is ClassificationLevel.CONFIDENTIAL

    def test_effective_level_unknown_type_fail_closed(self, db):
        with pytest.raises(ValueError):
            effective_level(db, resource_type="starship", resource_id="1")


# ── 降級申請 + 核准(doc 08 §7 變體 A、§8、§12)────────────────────────────────


class TestDeclassification:
    def _latched_conversation(self, db):
        admin = make_user(db, "boss", role="admin")
        conv = make_conversation(db, admin)
        latch_confidential(db, conv, admin)
        return admin, conv

    def _request(self, db, admin, conv) -> DeclassificationRequest:
        return create_declassification_request(
            db,
            resource_type="conversation",
            resource_id=str(conv.id),
            to_level="無機密",
            requested_by_admin_id=admin.id,
            reason="專案結案,內容已完成降密審查",
        )

    def test_non_admin_cannot_request(self, db):
        admin, conv = self._latched_conversation(db)
        pleb = make_user(db, "mallory", role="user")
        dev = make_user(db, "dev", role="developer")
        for actor in (pleb, dev):
            with pytest.raises(ValueError):
                create_declassification_request(
                    db,
                    resource_type="conversation",
                    resource_id=str(conv.id),
                    to_level="無機密",
                    requested_by_admin_id=actor.id,
                    reason="not allowed",
                )
        assert db.query(DeclassificationRequest).count() == 0

    def test_request_pending_by_default_with_levels(self, db):
        admin, conv = self._latched_conversation(db)
        req = self._request(db, admin, conv)
        assert req.status == "pending_supervisor"  # fail-closed 預設
        assert req.from_level == "機密"
        assert req.to_level == "無機密"
        assert req.requested_by_admin_id == admin.id
        assert req.approved_via is None
        assert req.decided_at is None

    def test_to_level_must_be_strictly_lower(self, db):
        admin, conv = self._latched_conversation(db)
        for same_or_higher in ("機密", "絕對機密"):
            with pytest.raises(ValueError):
                create_declassification_request(
                    db,
                    resource_type="conversation",
                    resource_id=str(conv.id),
                    to_level=same_or_higher,
                    requested_by_admin_id=admin.id,
                    reason="not a downgrade",
                )

    def test_requester_equals_approver_rejected(self, db):
        admin, conv = self._latched_conversation(db)
        grant_authority(db, admin)
        req = self._request(db, admin, conv)
        with pytest.raises(ValueError):
            decide_declassification(
                db,
                request_id=req.id,
                approver_user_id=admin.id,
                approve=True,
                via="in_system",
            )
        db.refresh(req)
        assert req.status == "pending_supervisor"

    def test_no_authority_stays_pending_fail_closed_with_audit(self, db):
        admin, conv = self._latched_conversation(db)
        req = self._request(db, admin, conv)
        outsider = make_user(db, "owner-without-authority", role="owner")
        assert has_declassification_authority(db, outsider.id) is False

        result = decide_declassification(
            db,
            request_id=req.id,
            approver_user_id=outsider.id,
            approve=True,
            via="in_system",
        )

        assert result.status == "pending_supervisor"
        db.refresh(conv)
        assert conv.classification_level == "機密"  # 未生效
        audit = (
            db.query(AuditLog)
            .filter(AuditLog.action == "supervisor_missing")
            .all()
        )
        assert len(audit) == 1

    def test_paper_via_requires_document_number(self, db):
        admin, conv = self._latched_conversation(db)
        req = self._request(db, admin, conv)
        recorder = make_user(db, "recorder", role="admin")
        grant_authority(db, recorder)
        with pytest.raises(ValueError):
            decide_declassification(
                db,
                request_id=req.id,
                approver_user_id=recorder.id,
                approve=True,
                via="recorded_paper_decision",
                paper_doc_no=None,
                authority_title_name="院長 王大明",
            )
        with pytest.raises(ValueError):
            decide_declassification(
                db,
                request_id=req.id,
                approver_user_id=recorder.id,
                approve=True,
                via="recorded_paper_decision",
                paper_doc_no="院授字第1130001號",
                authority_title_name=None,
            )
        db.refresh(req)
        assert req.status == "pending_supervisor"

    def test_invalid_via_fail_closed(self, db):
        admin, conv = self._latched_conversation(db)
        req = self._request(db, admin, conv)
        supervisor = make_user(db, "supervisor", role="admin")
        grant_authority(db, supervisor)
        with pytest.raises(ValueError):
            decide_declassification(
                db,
                request_id=req.id,
                approver_user_id=supervisor.id,
                approve=True,
                via="self_service",
            )

    def test_approve_in_system_lowers_exactly_once_with_event(self, db):
        admin, conv = self._latched_conversation(db)
        req = self._request(db, admin, conv)
        supervisor = make_user(db, "supervisor", role="admin")
        grant_authority(db, supervisor)
        before_events = db.query(ClassificationEvent).count()

        result = decide_declassification(
            db,
            request_id=req.id,
            approver_user_id=supervisor.id,
            approve=True,
            via="in_system",
            comment="同意降級",
        )

        assert result.status == "applied"
        assert result.approved_via == "in_system"
        assert result.supervisor_user_id == supervisor.id
        assert result.decided_at is not None
        db.refresh(conv)
        assert conv.classification_level == "無機密"
        assert conv.classified is False  # 鏡射:降到 無機密 → boolean 回 false
        lowering = (
            db.query(ClassificationEvent)
            .filter(ClassificationEvent.reason == "declassification_copy")
            .all()
        )
        assert len(lowering) == 1
        assert lowering[0].previous_level == "機密"
        assert lowering[0].new_level == "無機密"
        assert db.query(ClassificationEvent).count() == before_events + 1
        assert result.audit_event_ids  # audit trail 已掛回申請單

        # 恰好一次:同一申請單不可再裁決
        with pytest.raises(ValueError):
            decide_declassification(
                db,
                request_id=req.id,
                approver_user_id=supervisor.id,
                approve=True,
                via="in_system",
            )

    def test_reject_keeps_level(self, db):
        admin, conv = self._latched_conversation(db)
        req = self._request(db, admin, conv)
        supervisor = make_user(db, "supervisor", role="admin")
        grant_authority(db, supervisor)
        result = decide_declassification(
            db,
            request_id=req.id,
            approver_user_id=supervisor.id,
            approve=False,
            via="in_system",
            comment="降密審查未過",
        )
        assert result.status == "rejected"
        assert result.approved_via is None
        db.refresh(conv)
        assert conv.classification_level == "機密"
        assert conv.classified is True

    def test_paper_approve_records_authority_fields(self, db):
        admin, conv = self._latched_conversation(db)
        req = self._request(db, admin, conv)
        recorder = make_user(db, "recorder", role="admin")
        grant_authority(db, recorder)
        result = decide_declassification(
            db,
            request_id=req.id,
            approver_user_id=recorder.id,
            approve=True,
            via="recorded_paper_decision",
            paper_doc_no="院授字第1130001號",
            authority_title_name="院長 王大明",
        )
        assert result.status == "applied"
        assert result.approved_via == "recorded_paper_decision"
        assert result.authority_reference == "院授字第1130001號"
        assert result.authority_title_name == "院長 王大明"
        assert result.recorded_by_user_id == recorder.id
        db.refresh(conv)
        assert conv.classification_level == "無機密"

    def test_has_declassification_authority_respects_revocation(self, db):
        user = make_user(db, "holder", role="admin")
        assignment = grant_authority(db, user)
        assert has_declassification_authority(db, user.id) is True
        assignment.is_active = False
        assignment.revoked_at = datetime.now(timezone.utc)
        db.commit()
        assert has_declassification_authority(db, user.id) is False


# ── migration chain ───────────────────────────────────────────────────────────


class TestMigrationChainSlice3a:
    def test_single_head_in_r1_namespace(self):
        versions = Path(__file__).resolve().parents[1] / "migrations" / "versions"
        revisions: set[str] = set()
        down_revisions: set[str] = set()
        rev_re = re.compile(r'^revision(?::\s*str)?\s*=\s*["\']([^"\']+)["\']', re.M)
        down_re = re.compile(
            r'^down_revision(?::[^=]+)?\s*=\s*(?:["\']([^"\']+)["\']|None)', re.M
        )
        for path in sorted(versions.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            rev = rev_re.search(text)
            down = down_re.search(text)
            assert rev, f"{path.name} 缺 revision 宣告"
            revisions.add(rev.group(1))
            if down and down.group(1):
                down_revisions.add(down.group(1))
        heads = revisions - down_revisions
        # 不釘死特定 head id(每個 slice 加 migration 就過期);與
        # test_task_trace_schema 同式:守恆兩不變量=恰一 head + r1_ 命名空間。
        assert len(heads) == 1, f"alembic head 應唯一,實得 {heads}"
        assert next(iter(heads)).startswith("r1_"), (
            f"head 應屬 r1_ 命名空間,實得 {heads}"
        )
