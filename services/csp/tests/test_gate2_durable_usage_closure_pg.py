"""True PostgreSQL proof for durable Task usage closure and poison isolation."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.model_registry import ModelRegistry
from app.models.audit_log import AuditLog
from app.models.task import Task, TaskRun
from app.models.token_usage import TokenUsage
from app.models.trace_span import TraceSpan
from app.models.user import User
from app.services import usage_writer
from app.services.proxy.closure import (
    TaskCallClosure,
    UsageRecordData,
    persist_task_call_closure,
)

_DSN = os.environ.get("ANILA_GATE2_LEDGER_PG_URL")
pytestmark = pytest.mark.skipif(
    not _DSN, reason="ANILA_GATE2_LEDGER_PG_URL is not set"
)


def _seed(factory):
    db = factory()
    suffix = uuid.uuid4().hex[:12]
    user = User(
        username=f"gate2-durable-{suffix}",
        role="user",
        hashed_password="synthetic-not-a-secret",
        is_active=True,
        is_approved=True,
    )
    model = ModelRegistry(
        name=f"gate2-durable-model-{suffix}",
        display_name="Gate2 durable model",
        model_type="llm",
        endpoint_url="http://durable-model:8080",
        is_active=True,
        classification_ceiling="機密",
    )
    db.add_all([user, model])
    db.flush()
    task = Task(
        title="true PG durable closure",
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
    return db, user, model, task, run


def _cleanup(db, *, task_id, user_id, model_id):
    db.rollback()
    # audit_logs is append-only since r1_0041: csp_app has no DELETE on it and a
    # trigger rejects the statement even for the admin role, so this cleanup
    # failed two different ways depending on which DSN the job used. Rows here
    # are already scoped to this run's own task id, so leaving them cannot
    # affect another test — and deleting them would be asserting the absence of
    # the property the migration exists to create.
    db.query(TraceSpan).filter_by(task_id=task_id).delete(synchronize_session=False)
    run = db.query(TaskRun).filter_by(task_id=task_id).one_or_none()
    if run is not None:
        run.usage_record_id = None
        db.flush()
    db.query(TokenUsage).filter(
        (TokenUsage.task_id == task_id) | (TokenUsage.user_id == user_id)
    ).delete(synchronize_session=False)
    task = db.get(Task, task_id)
    if task is not None:
        db.delete(task)
    model = db.get(ModelRegistry, model_id)
    if model is not None:
        db.delete(model)
    # r1_0041 leaves audit_logs.actor_user_id without ON DELETE, so the actor
    # reference is nulled in the application layer before the user row goes
    # away — that is how the audit history survives a hard delete (canonical
    # implementation: api/users.py "manual cleanup #2").
    db.query(AuditLog).filter(AuditLog.actor_user_id == user_id).update(
        {"actor_user_id": None}, synchronize_session=False
    )
    user = db.get(User, user_id)
    if user is not None:
        db.delete(user)
    db.commit()


def test_pg_closure_commits_usage_run_and_trace_exactly_once() -> None:
    engine = create_engine(_DSN, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db, user, model, task, run = _seed(factory)
    closure = TaskCallClosure(
        closure_id=uuid.uuid4().hex,
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
            prompt_tokens=11,
            completion_tokens=13,
            total_tokens=24,
        ),
        classification_level="機密",
        callsite="csp.chat_model",
    )
    try:
        usage_id = persist_task_call_closure(db, closure)
        assert persist_task_call_closure(db, closure) == usage_id
        verify = factory()
        try:
            persisted_run = verify.get(TaskRun, run.id)
            assert persisted_run.status == "completed"
            assert persisted_run.usage_record_id == usage_id
            assert verify.query(TokenUsage).filter_by(task_id=task.id).count() == 1
            spans = verify.query(TraceSpan).filter_by(task_id=task.id).all()
            assert len(spans) == 2
            assert {span.trace_id for span in spans} == {task.trace_id}
            child = next(span for span in spans if span.parent_span_id)
            assert child.parent_span_id == closure.closure_id
        finally:
            verify.close()
    finally:
        _cleanup(db, task_id=task.id, user_id=user.id, model_id=model.id)
        db.close()
        engine.dispose()


def test_pg_legacy_batch_poison_does_not_drop_valid_sibling(monkeypatch) -> None:
    engine = create_engine(_DSN, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db, user, model, task, run = _seed(factory)
    monkeypatch.setattr(usage_writer, "SessionLocal", factory)
    valid = {
        "api_key_id": None,
        "user_id": user.id,
        "department_id": None,
        "model_id": model.id,
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "total_tokens": 2,
        "request_timestamp": datetime.now(timezone.utc),
        "request_type": "chat",
        "legacy_runtime_call": True,
    }
    poison = {**valid, "model_id": 2_147_483_647}
    try:
        asyncio.run(usage_writer._flush_batch([valid, poison]))
        verify = factory()
        try:
            rows = verify.query(TokenUsage).filter_by(user_id=user.id).all()
            assert len(rows) == 1
            assert rows[0].model_id == model.id
        finally:
            verify.close()
    finally:
        _cleanup(db, task_id=task.id, user_id=user.id, model_id=model.id)
        db.close()
        engine.dispose()
