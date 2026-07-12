"""Gate 2 G6/G6b transaction, crash, retry and idempotency evidence."""
from __future__ import annotations

import os
os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from datetime import datetime, timedelta, timezone
import pytest
from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from app.models.audit_log import AuditLog
from app.models.policy_decision import PolicyDecision
from app.models.task import Task, TaskRun
from app.middleware.caller import Caller
from app.modules import tasks as task_module
from app.services.proxy import task_link
from app.services.proxy.task_link import (
    TaskRunContext,
    finalize_task_preflight,
    finalize_task_run,
    record_task_policy_decision,
    reconcile_stale_task_runs,
)
from tests.conftest import make_user


def _running(db: Session, username: str = "ledger_owner") -> tuple[Task, TaskRun]:
    user = make_user(db, username=username)
    task = Task(
        title="ledger", task_type="query", requester_user_id=user.id,
        status="running", classification_level="機密",
    )
    db.add(task)
    db.flush()
    run = TaskRun(
        task_id=task.id, run_sequence=1, dispatch_target="model",
        status="running", classification_level="機密",
    )
    db.add(run)
    db.commit()
    return task, run


def test_policy_block_task_run_decision_and_audit_are_one_transaction(
    db: Session,
) -> None:
    task, run = _running(db)
    ctx = TaskRunContext(task.id, task.trace_id, run.id)
    record_task_policy_decision(
        db,
        task_ctx=ctx,
        action="model.invoke",
        resource_type="model",
        resource_id="7",
        decision="deny",
        actor_id=str(task.requester_user_id),
        reason="分類上限不足",
        block=True,
    )
    db.expire_all()
    assert db.get(Task, task.id).status == "blocked_by_policy"
    assert db.get(TaskRun, run.id).status == "failed"
    assert db.query(PolicyDecision).filter_by(task_id=task.id, decision="deny").count() == 1
    assert db.query(AuditLog).filter_by(
        resource_type="task", resource_id=str(task.id), action="task.policy.blocked"
    ).count() == 1

    # Retry is idempotent: the terminal run prevents duplicate ledger rows.
    record_task_policy_decision(
        db,
        task_ctx=ctx,
        action="model.invoke",
        resource_type="model",
        resource_id="7",
        decision="deny",
        actor_id=str(task.requester_user_id),
        reason="分類上限不足",
        block=True,
    )
    assert db.query(PolicyDecision).filter_by(task_id=task.id, decision="deny").count() == 1


def test_commit_crash_rolls_back_every_governance_row(db: Session) -> None:
    task, run = _running(db, "ledger_crash")
    ctx = TaskRunContext(task.id, task.trace_id, run.id)

    def crash(_session):
        raise RuntimeError("synthetic crash before commit")

    event.listen(db, "before_commit", crash, once=True)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        record_task_policy_decision(
            db,
            task_ctx=ctx,
            action="model.invoke",
            resource_type="model",
            resource_id="8",
            decision="deny",
            actor_id=str(task.requester_user_id),
            reason="crash test",
            block=True,
        )
    db.rollback()
    db.expire_all()
    assert db.get(Task, task.id).status == "running"
    assert db.get(TaskRun, run.id).status == "running"
    assert db.query(PolicyDecision).filter_by(task_id=task.id).count() == 0
    assert db.query(AuditLog).filter_by(
        resource_type="task", resource_id=str(task.id)
    ).count() == 0


def test_success_failure_cancel_finalizer_converges_task_and_is_idempotent(
    db: Session, db_engine, monkeypatch
) -> None:
    task, run = _running(db, "ledger_finish")
    factory = sessionmaker(bind=db_engine)
    monkeypatch.setattr(task_link, "SessionLocal", factory)
    finalize_task_run(run.id, "completed")
    finalize_task_run(run.id, "completed")
    db.expire_all()
    assert db.get(Task, task.id).status == "completed"
    assert db.get(TaskRun, run.id).status == "completed"
    assert db.query(AuditLog).filter_by(
        resource_type="task", resource_id=str(task.id), action="task.run.finished"
    ).count() == 1


@pytest.mark.parametrize("terminal", ["failed", "blocked_by_policy"])
def test_preflight_without_run_closes_task_with_decision_and_audit(
    db: Session, terminal: str
) -> None:
    user = make_user(db, username=f"preflight_{terminal}")
    task = Task(
        title="preflight", task_type="query", requester_user_id=user.id,
        status="draft",
    )
    db.add(task)
    db.commit()
    finalize_task_preflight(
        db,
        task_id=task.id,
        terminal_status=terminal,
        action="task.run",
        resource_type="retrieval",
        resource_id=None,
        actor_id=str(user.id),
        reason="clearance denied" if terminal == "blocked_by_policy" else "retrieval failed",
    )
    db.expire_all()
    assert db.get(Task, task.id).status == terminal
    assert db.query(PolicyDecision).filter_by(task_id=task.id, decision="deny").count() == 1
    assert db.query(AuditLog).filter_by(
        resource_type="task", resource_id=str(task.id),
        action=f"task.preflight.{terminal}",
    ).count() == 1


def test_crash_reconciler_closes_only_stale_runs_and_retry_is_idempotent(
    db: Session,
) -> None:
    task, run = _running(db, "ledger_stale")
    run.started_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit()
    assert reconcile_stale_task_runs(db, stale_after_seconds=600) == 1
    assert reconcile_stale_task_runs(db, stale_after_seconds=600) == 0
    db.expire_all()
    assert db.get(Task, task.id).status == "failed"
    assert db.get(TaskRun, run.id).error["code"] == "stale_runtime"
    assert db.query(PolicyDecision).filter_by(
        task_id=task.id, decision="deny"
    ).count() == 1
    assert db.query(AuditLog).filter_by(
        resource_type="task", resource_id=str(task.id),
        action="task.run.reconciled",
    ).count() == 1


def test_begin_rolls_back_if_task_run_does_not_transition_task(
    db: Session, monkeypatch
) -> None:
    user = make_user(db, username="ledger_transition_guard")
    task = Task(
        title="transition guard",
        task_type="query",
        requester_user_id=user.id,
        status="draft",
    )
    db.add(task)
    db.commit()

    def broken_start(session, *, task, dispatch_target, commit=False):
        run = TaskRun(
            task_id=task.id,
            run_sequence=1,
            dispatch_target=dispatch_target,
            status="running",
            started_at=datetime.now(timezone.utc),
        )
        session.add(run)
        session.flush()
        return run

    monkeypatch.setattr(task_module, "start_task_run", broken_start)
    with pytest.raises(HTTPException, match="原子轉入 running"):
        task_link.begin_task_run(
            db,
            caller=Caller(user=user, api_key_id=None),
            request_headers={"X-ANILA-Task-Id": str(task.id)},
            dispatch_target="model",
            resource_type="model",
            resource_id="99",
        )

    db.expire_all()
    assert db.get(Task, task.id).status == "draft"
    assert db.query(TaskRun).filter_by(task_id=task.id).count() == 0
    assert db.query(PolicyDecision).filter_by(task_id=task.id).count() == 0
    assert db.query(AuditLog).filter_by(
        resource_type="task", resource_id=str(task.id)
    ).count() == 0


def test_terminal_run_rejects_late_allow_without_duplicate_ledger(
    db: Session,
) -> None:
    task, run = _running(db, "ledger_terminal_allow")
    task.status = "cancelled"
    run.status = "cancelled"
    run.finished_at = datetime.now(timezone.utc)
    db.commit()
    ctx = TaskRunContext(task.id, task.trace_id, run.id)

    with pytest.raises(HTTPException, match="終態後"):
        record_task_policy_decision(
            db,
            task_ctx=ctx,
            action="model.invoke",
            resource_type="model",
            resource_id="7",
            decision="allow",
            actor_id=str(task.requester_user_id),
        )

    assert db.query(PolicyDecision).filter_by(task_id=task.id).count() == 0
    assert db.query(AuditLog).filter_by(
        resource_type="task", resource_id=str(task.id)
    ).count() == 0
