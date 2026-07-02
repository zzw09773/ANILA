# -*- coding: utf-8 -*-
"""Slice 2a — Task / Trace / Policy 六表 schema 基礎測試。

依 docs/anila-redesign-docs/01-domain-model.md(Task 是主脊椎、SourceSnapshot
三規則)、03(PolicyDecision 九動作 enum、append-only)、05 §6(13 個必備
agent span type,Slice 4 才收斂為封閉 enum)、09(span event schema)。

涵蓋:
- create_all 下六表 CRUD smoke(最小列插入 + 預設值)
- FK 完整性:task → task_runs / source_snapshots、citation → snapshot
  (ORM cascade;SQLite 不開 FK pragma,以 relationship cascade 驗證)
- 唯一性:tasks.trace_id、trace_spans (trace_id, span_id)
- 契約 enum:Task 狀態機 round-trip、PolicyDecision 九動作、決策三值
- classification_level 新列預設 = 無機密
- migration chain:恰好一個 alembic head,且屬 r1_ 命名空間(純文字解析,免 DB)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.source_snapshot import Citation, SourceSnapshot
from app.models.policy_decision import PolicyDecision
from app.models.task import Task, TaskRun
from app.models.trace_span import TraceSpan
from app.schemas.contracts import ClassificationLevel
from app.schemas.contracts.policy import (
    PolicyAction,
    PolicyActorType,
    PolicyDecisionOut,
    PolicyDecisionVerdict,
)
from app.schemas.contracts.tasks import (
    CitationOut,
    CitationUsedBy,
    DispatchTarget,
    RequestedOutputType,
    SnapshotOrigin,
    SourceScope,
    SourceSnapshotIn,
    SourceSnapshotOut,
    TaskCreate,
    TaskOut,
    TaskRunOut,
    TaskRunStatus,
    TaskStatus,
    TaskType,
)
from app.schemas.contracts.traces import (
    REQUIRED_AGENT_SPAN_TYPES,
    SpanProducer,
    SpanStatus,
    TraceSpanIn,
    TraceSpanOut,
)
from tests.conftest import make_user

UNCLASSIFIED = "無機密"


def make_task(db, user, **overrides) -> Task:
    fields = dict(
        title="測試任務",
        task_type=TaskType.QUERY.value,
        requester_user_id=user.id,
    )
    fields.update(overrides)
    task = Task(**fields)
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


# ── CRUD smoke + 預設值 ───────────────────────────────────────────────────────


class TestTaskCrud:
    def test_minimal_task_row_defaults(self, db):
        user = make_user(db)
        task = make_task(db, user)
        assert task.id is not None
        assert task.status == TaskStatus.DRAFT.value
        assert task.source_scope == SourceScope.NONE.value
        assert task.legacy_runtime_call is False
        assert task.classification_level == UNCLASSIFIED
        assert task.created_at is not None
        # doc 01 驗收:建立 Task 必產生 trace_id
        assert task.trace_id

    def test_task_trace_id_unique(self, db):
        user = make_user(db)
        make_task(db, user, trace_id="trace-dup")
        with pytest.raises(IntegrityError):
            make_task(db, user, trace_id="trace-dup")
        db.rollback()

    def test_task_run_row_and_fk(self, db):
        user = make_user(db)
        task = make_task(db, user)
        run = TaskRun(
            task_id=task.id,
            run_sequence=1,
            dispatch_target=DispatchTarget.MODEL.value,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        assert run.status == TaskRunStatus.QUEUED.value
        assert run.classification_level == UNCLASSIFIED
        assert run.usage_record_id is None
        assert run.error is None
        assert [r.id for r in task.runs] == [run.id]

    def test_task_delete_cascades_runs_and_snapshots(self, db):
        user = make_user(db)
        task = make_task(db, user)
        db.add(TaskRun(task_id=task.id, run_sequence=1,
                       dispatch_target=DispatchTarget.AGENT.value))
        db.add(SourceSnapshot(task_id=task.id,
                              origin=SnapshotOrigin.COLLECTION.value))
        db.commit()
        db.delete(task)
        db.commit()
        assert db.query(TaskRun).count() == 0
        assert db.query(SourceSnapshot).count() == 0


class TestSourceSnapshotAndCitation:
    def test_snapshot_defaults(self, db):
        user = make_user(db)
        task = make_task(db, user)
        snap = SourceSnapshot(
            task_id=task.id,
            origin=SnapshotOrigin.COLLECTION.value,
            source_scope=SourceScope.PROJECT.value,
            collection_ids=[1, 2],
            document_ids=[10],
            chunk_ids=["c1", "c2"],
            content_hash="a" * 64,
        )
        db.add(snap)
        db.commit()
        db.refresh(snap)
        assert snap.classification_level == UNCLASSIFIED
        assert snap.collection_ids == [1, 2]
        assert snap.created_at is not None

    def test_citation_points_at_snapshot_and_cascades(self, db):
        user = make_user(db)
        task = make_task(db, user)
        snap = SourceSnapshot(task_id=task.id,
                              origin=SnapshotOrigin.DOCUMENT.value)
        db.add(snap)
        db.commit()
        cite = Citation(
            source_snapshot_id=snap.id,
            chunk_id="chunk-1",
            quote_preview="第一條……",
            used_by=CitationUsedBy.ANSWER.value,
        )
        db.add(cite)
        db.commit()
        db.refresh(cite)
        assert cite.classification_level == UNCLASSIFIED
        assert [c.id for c in snap.citations] == [cite.id]
        db.delete(snap)
        db.commit()
        assert db.query(Citation).count() == 0


class TestPolicyDecisionCrud:
    def test_policy_decision_row(self, db):
        user = make_user(db)
        task = make_task(db, user)
        row = PolicyDecision(
            task_id=task.id,
            actor_type=PolicyActorType.USER.value,
            actor_id=user.id,
            action=PolicyAction.TASK_RUN.value,
            resource_type="task",
            resource_id=str(task.id),
            decision=PolicyDecisionVerdict.ALLOW.value,
            reason="clearance >= resource level",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        assert row.classification_level == UNCLASSIFIED
        assert row.matched_policy_ids == []
        assert row.created_at is not None


class TestTraceSpanCrud:
    def test_trace_span_row(self, db):
        span = TraceSpan(
            trace_id="tr-1",
            span_id="sp-1",
            span_type="agent.run.started",
            name="agent run",
            producer=SpanProducer.AGENT.value,
        )
        db.add(span)
        db.commit()
        db.refresh(span)
        assert span.status == SpanStatus.OK.value
        assert span.classification_level == UNCLASSIFIED
        assert span.parent_span_id is None

    def test_trace_span_unique_per_trace(self, db):
        db.add(TraceSpan(trace_id="tr-1", span_id="sp-1",
                         span_type="agent.run.started", name="a",
                         producer=SpanProducer.ROUTER.value))
        db.commit()
        db.add(TraceSpan(trace_id="tr-1", span_id="sp-1",
                         span_type="agent.run.finished", name="b",
                         producer=SpanProducer.ROUTER.value))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        # 不同 trace 同 span_id 合法
        db.add(TraceSpan(trace_id="tr-2", span_id="sp-1",
                         span_type="agent.run.started", name="c",
                         producer=SpanProducer.PROXY.value))
        db.commit()


# ── 契約 enum ─────────────────────────────────────────────────────────────────


class TestContracts:
    def test_task_status_enum_exact_values(self):
        assert [s.value for s in TaskStatus] == [
            "draft", "submitted", "policy_checking", "source_resolving",
            "running", "waiting_for_user", "completed", "failed",
            "cancelled", "blocked_by_policy",
        ]

    def test_source_scope_enum_exact_values(self):
        assert {s.value for s in SourceScope} == {
            "none", "personal", "project", "organization",
            "registered_service",
        }

    def test_task_status_round_trip_via_contract(self, db):
        user = make_user(db)
        task = make_task(db, user, status=TaskStatus.RUNNING.value)
        out = TaskOut.model_validate(task)
        assert out.status is TaskStatus.RUNNING
        assert out.classification_level is ClassificationLevel.UNCLASSIFIED
        assert out.trace_id == task.trace_id

    def test_task_create_rejects_unknown_enum(self):
        with pytest.raises(Exception):
            TaskCreate(title="x", task_type="not-a-type",
                       source_scope="project")

    def test_task_run_out_round_trip(self, db):
        user = make_user(db)
        task = make_task(db, user)
        run = TaskRun(task_id=task.id, run_sequence=2,
                      dispatch_target=DispatchTarget.STUDIO.value,
                      status=TaskRunStatus.COMPLETED.value)
        db.add(run)
        db.commit()
        db.refresh(run)
        out = TaskRunOut.model_validate(run)
        assert out.dispatch_target is DispatchTarget.STUDIO
        assert out.status is TaskRunStatus.COMPLETED

    def test_policy_action_exact_nine_values(self):
        assert [a.value for a in PolicyAction] == [
            "task.run",
            "model.invoke",
            "agent.invoke",
            "service.launch",
            "artifact.export",
            "collection.read",
            "classification.downgrade_request",
            "registry.create",
            "registry.approve",
        ]

    def test_policy_decision_verdict_exact_values(self):
        assert [d.value for d in PolicyDecisionVerdict] == [
            "allow", "deny", "require_approval",
        ]

    def test_policy_decision_out_round_trip(self, db):
        row = PolicyDecision(
            actor_type=PolicyActorType.SERVICE.value,
            action=PolicyAction.AGENT_INVOKE.value,
            resource_type="agent",
            resource_id="42",
            decision=PolicyDecisionVerdict.DENY.value,
            reason="ceiling exceeded",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        out = PolicyDecisionOut.model_validate(row)
        assert out.action is PolicyAction.AGENT_INVOKE
        assert out.decision is PolicyDecisionVerdict.DENY
        assert out.task_id is None

    def test_required_agent_span_types_thirteen(self):
        assert len(REQUIRED_AGENT_SPAN_TYPES) == 13
        assert REQUIRED_AGENT_SPAN_TYPES[0] == "agent.run.started"
        assert REQUIRED_AGENT_SPAN_TYPES[-1] == "agent.run.finished"
        assert "agent.error" in REQUIRED_AGENT_SPAN_TYPES

    def test_trace_span_in_accepts_doc09_shape(self):
        span = TraceSpanIn(
            span_id="sp-9",
            parent_span_id=None,
            trace_id="tr-9",
            span_type="agent.tool_call.started",
            name="tool call",
            status=SpanStatus.OK,
            attributes={"tool_name": "search"},
        )
        assert span.span_type == "agent.tool_call.started"
        # span_type 是開放字串(封閉 enum 保留給 Slice 4)
        open_span = TraceSpanIn(span_id="sp-10", span_type="custom.span",
                                name="x")
        assert open_span.status is SpanStatus.OK

    def test_trace_span_out_round_trip(self, db):
        span = TraceSpan(trace_id="tr-3", span_id="sp-3",
                         span_type="agent.retrieval.finished", name="ret",
                         producer=SpanProducer.MODEL.value)
        db.add(span)
        db.commit()
        db.refresh(span)
        out = TraceSpanOut.model_validate(span)
        assert out.producer is SpanProducer.MODEL
        assert out.status is SpanStatus.OK

    def test_source_snapshot_contracts(self, db):
        payload = SourceSnapshotIn(
            origin=SnapshotOrigin.COLLECTION,
            source_scope=SourceScope.ORGANIZATION,
            collection_ids=[3],
            document_ids=[7, 8],
            chunk_ids=["k1"],
        )
        # classification_level 缺省 = None:留給 service 層以 max 規則計算
        assert payload.classification_level is None
        user = make_user(db)
        task = make_task(db, user)
        snap = SourceSnapshot(task_id=task.id,
                              origin=SnapshotOrigin.NONE.value)
        db.add(snap)
        db.commit()
        db.refresh(snap)
        out = SourceSnapshotOut.model_validate(snap)
        assert out.origin is SnapshotOrigin.NONE
        assert out.classification_level is ClassificationLevel.UNCLASSIFIED

    def test_citation_out_round_trip(self, db):
        user = make_user(db)
        task = make_task(db, user)
        snap = SourceSnapshot(task_id=task.id,
                              origin=SnapshotOrigin.UPLOAD.value)
        db.add(snap)
        db.commit()
        cite = Citation(source_snapshot_id=snap.id, chunk_id="k9",
                        used_by=CitationUsedBy.ARTIFACT.value)
        db.add(cite)
        db.commit()
        db.refresh(cite)
        out = CitationOut.model_validate(cite)
        assert out.used_by is CitationUsedBy.ARTIFACT
        assert out.chunk_id == "k9"

    def test_requested_output_type_values(self):
        assert {v.value for v in RequestedOutputType} == {
            "answer", "report", "slides", "mindmap", "infographic",
            "datatable", "service_launch",
        }


# ── migration chain(純文字解析,免 DB)───────────────────────────────────────


class TestMigrationChain:
    def test_single_head_in_r1_namespace(self):
        versions = Path(__file__).resolve().parents[1] / "migrations" / "versions"
        revisions: dict[str, str] = {}
        down_revisions: set[str] = set()
        rev_re = re.compile(
            r'^revision(?::\s*str)?\s*=\s*["\']([^"\']+)["\']', re.M
        )
        down_re = re.compile(
            r'^down_revision(?::[^=]+)?\s*=\s*(?:["\']([^"\']+)["\']|None)',
            re.M,
        )
        for path in sorted(versions.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            rev = rev_re.search(text)
            down = down_re.search(text)
            assert rev, f"{path.name} 缺 revision 宣告"
            revisions[rev.group(1)] = path.name
            if down and down.group(1):
                down_revisions.add(down.group(1))
        heads = set(revisions) - down_revisions
        # 不釘死特定 head id(每加一個 migration 就過期);守住兩個不變量:
        # 恰一個 head + head 屬本分支 r1_ 命名空間(避免與 main 系撞號)。
        assert len(heads) == 1, f"alembic head 應唯一,實得 {heads}"
        assert next(iter(heads)).startswith("r1_"), (
            f"head 應屬 r1_ 命名空間,實得 {heads}"
        )
