from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import OperationalError

from app.models.task import Task, TaskRun
from app.models.token_usage import TokenUsage
from app.models.trace_span import TraceSpan
from app.services.proxy.closure import (
    TaskCallClosure,
    UsageRecordData,
    persist_task_call_closure,
)
from tests.conftest import make_model, make_user


def _seed(db):
    user = make_user(db, username="durable-usage-user")
    model = make_model(db, name="durable-usage-model")
    task = Task(
        title="durable closure",
        task_type="query",
        requester_user_id=user.id,
        status="running",
        classification_level="機密",
    )
    db.add(task)
    db.flush()
    run = TaskRun(
        task_id=task.id,
        run_sequence=1,
        dispatch_target="model",
        status="running",
        classification_level="機密",
    )
    db.add(run)
    db.commit()
    return user, model, task, run


def _closure(user, model, task, run, *, closure_id="closure-atomic"):
    return TaskCallClosure(
        closure_id=closure_id,
        task_id=task.id,
        task_run_id=run.id,
        trace_id=task.trace_id,
        started_at=datetime.now(timezone.utc),
        status="completed",
        is_agent=False,
        target_id=model.id,
        target_name=model.name,
        usage=UsageRecordData(
            api_key_id=None,
            user_id=user.id,
            department_id=None,
            model_id=model.id,
            prompt_tokens=3,
            completion_tokens=4,
            total_tokens=7,
        ),
        classification_level="機密",
        callsite="csp.chat_model",
    )


def test_commit_failure_rolls_back_usage_run_and_spans(db, monkeypatch):
    user, model, task, run = _seed(db)
    closure = _closure(user, model, task, run)
    real_commit = db.commit

    def fail_commit():
        raise RuntimeError("synthetic crash before commit")

    monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        persist_task_call_closure(db, closure)
    monkeypatch.setattr(db, "commit", real_commit)

    db.expire_all()
    assert db.get(TaskRun, run.id).status == "running"
    assert db.get(TaskRun, run.id).usage_record_id is None
    assert db.query(TokenUsage).filter_by(task_id=task.id).count() == 0
    assert db.query(TraceSpan).filter_by(task_id=task.id).count() == 0


def test_transient_retry_is_atomic_idempotent_and_links_child_span(db, monkeypatch):
    user, model, task, run = _seed(db)
    closure = _closure(user, model, task, run, closure_id="closure-retry")
    real_commit = db.commit
    attempts = 0

    def transient_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OperationalError("COMMIT", {}, RuntimeError("connection reset"))
        real_commit()

    monkeypatch.setattr(db, "commit", transient_once)
    usage_id = persist_task_call_closure(db, closure)
    assert attempts == 2
    assert usage_id is not None

    # Same call id is an idempotent readback, not a second usage write.
    assert persist_task_call_closure(db, closure) == usage_id
    db.expire_all()
    persisted_run = db.get(TaskRun, run.id)
    assert persisted_run.status == "completed"
    assert persisted_run.usage_record_id == usage_id
    usage = db.get(TokenUsage, usage_id)
    assert usage.trace_id == task.trace_id
    assert db.query(TokenUsage).filter_by(task_id=task.id).count() == 1
    spans = db.query(TraceSpan).filter_by(task_id=task.id).all()
    assert len(spans) == 2
    parent = next(span for span in spans if span.parent_span_id is None)
    child = next(span for span in spans if span.parent_span_id is not None)
    assert child.parent_span_id == parent.span_id == closure.closure_id
    assert {span.trace_id for span in spans} == {task.trace_id}


def test_nested_retrieval_and_memory_usage_share_task_trace_without_finalizing(db):
    user, model, task, run = _seed(db)
    for index, callsite in enumerate(
        ("csp.server_retrieval_embedding", "csp.memory_embedding"), start=1
    ):
        nested = _closure(
            user, model, task, run, closure_id=f"nested-closure-{index}"
        )
        nested = TaskCallClosure(
            **{
                **nested.__dict__,
                "finalize_run": False,
                "callsite": callsite,
            }
        )
        persist_task_call_closure(db, nested)

    db.expire_all()
    assert db.get(TaskRun, run.id).status == "running"
    assert db.get(TaskRun, run.id).usage_record_id is None
    usage_rows = db.query(TokenUsage).filter_by(task_id=task.id).all()
    assert len(usage_rows) == 2
    assert {row.trace_id for row in usage_rows} == {task.trace_id}
    spans = db.query(TraceSpan).filter_by(task_id=task.id).all()
    assert len(spans) == 4
    assert {span.trace_id for span in spans} == {task.trace_id}
    assert {span.attributes["callsite"] for span in spans} == {
        "csp.server_retrieval_embedding",
        "csp.memory_embedding",
    }
