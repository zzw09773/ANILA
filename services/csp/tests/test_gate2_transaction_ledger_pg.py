"""True-PostgreSQL race proof for Gate 2 governance finalization."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.audit_log import AuditLog
from app.models.policy_decision import PolicyDecision
from app.models.model_registry import ModelRegistry
from app.models.task import Task, TaskRun
from app.models.user import User
from app.services.proxy.task_link import (
    TaskRunContext,
    finalize_task_run_in_session,
    record_task_policy_decision,
)
from app.services.proxy import service as proxy_service

_DSN = os.environ.get("ANILA_GATE2_LEDGER_PG_URL")
pytestmark = pytest.mark.skipif(
    not _DSN, reason="ANILA_GATE2_LEDGER_PG_URL is not set"
)


def test_concurrent_policy_block_creates_exactly_one_atomic_ledger() -> None:
    engine = create_engine(_DSN, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    seed = factory()
    try:
        user = User(
            username=f"gate2-ledger-{suffix}", role="user",
            hashed_password="synthetic-not-a-secret", is_active=True,
            is_approved=True,
        )
        seed.add(user)
        seed.flush()
        task = Task(
            title="pg race", task_type="query", requester_user_id=user.id,
            status="running", classification_level="機密",
        )
        seed.add(task)
        seed.flush()
        run = TaskRun(
            task_id=task.id, run_sequence=1, dispatch_target="model",
            status="running", classification_level="機密",
        )
        seed.add(run)
        seed.commit()
        ctx = TaskRunContext(task.id, task.trace_id, run.id)

        def finalize() -> None:
            db = factory()
            try:
                record_task_policy_decision(
                    db,
                    task_ctx=ctx,
                    action="model.invoke",
                    resource_type="model",
                    resource_id="777",
                    decision="deny",
                    actor_id=str(user.id),
                    reason="synthetic concurrent ceiling deny",
                    block=True,
                )
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _: finalize(), range(2)))

        verify = factory()
        try:
            assert verify.get(Task, task.id).status == "blocked_by_policy"
            assert verify.get(TaskRun, run.id).status == "failed"
            assert verify.query(PolicyDecision).filter_by(
                task_id=task.id, decision="deny"
            ).count() == 1
            assert verify.query(AuditLog).filter_by(
                resource_type="task", resource_id=str(task.id),
                action="task.policy.blocked",
            ).count() == 1
            verify.query(AuditLog).filter_by(
                resource_type="task", resource_id=str(task.id)
            ).delete(synchronize_session=False)
            verify.delete(verify.get(Task, task.id))
            verify.delete(verify.get(User, user.id))
            verify.commit()
        finally:
            verify.close()
    finally:
        seed.close()
        engine.dispose()


class _PgProxyResponse:
    status_code = 200
    headers = {"content-type": "application/json"}
    text = '{"choices":[{"message":{"content":"ok"}}]}'

    def json(self):
        return {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }


class _PgProxyClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        return _PgProxyResponse()


@pytest.mark.asyncio
async def test_same_pg_session_holds_admission_locks_and_finalizes_without_deadlock(
    monkeypatch,
) -> None:
    engine = create_engine(
        _DSN,
        pool_pre_ping=True,
        connect_args={"options": "-c lock_timeout=2000ms"},
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    suffix = uuid.uuid4().hex[:12]
    try:
        user = User(
            username=f"gate2-proxy-{suffix}",
            role="user",
            hashed_password="synthetic-not-a-secret",
            is_active=True,
            is_approved=True,
        )
        model = ModelRegistry(
            name=f"gate2-proxy-model-{suffix}",
            display_name="Gate2 PG model",
            model_type="llm",
            endpoint_url="http://pg-proxy-model:8080",
            is_active=True,
            classification_ceiling="機密",
        )
        db.add_all([user, model])
        db.flush()
        task = Task(
            title="pg proxy lock",
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

        async def ignore_usage(**kwargs):
            return None

        monkeypatch.setattr(proxy_service, "_guard_outbound", lambda *a, **k: None)
        monkeypatch.setattr(proxy_service.httpx, "AsyncClient", _PgProxyClient)
        monkeypatch.setattr(
            proxy_service, "enqueue_usage_task_linked", ignore_usage
        )
        result = await proxy_service.proxy_request(
            model=model,
            api_key_id=None,
            user_id=user.id,
            department_id=None,
            request_body={"model": model.name, "messages": []},
            endpoint_path="/v1/chat/completions",
            task_id=task.id,
            task_trace_id=task.trace_id,
            task_run_id=run.id,
            inference_callsite_id="csp.chat_model",
            governance_db=db,
            admitted_classification_level="機密",
        )
        assert result["usage"]["total_tokens"] == 2
        db.expire_all()
        assert db.get(Task, task.id).status == "completed"
        assert db.get(TaskRun, run.id).status == "completed"
        assert db.query(AuditLog).filter_by(
            resource_type="task",
            resource_id=str(task.id),
            action="task.run.finished",
        ).count() == 1

        db.query(AuditLog).filter_by(
            resource_type="task", resource_id=str(task.id)
        ).delete(synchronize_session=False)
        db.delete(db.get(Task, task.id))
        db.delete(db.get(ModelRegistry, model.id))
        db.delete(db.get(User, user.id))
        db.commit()
    finally:
        db.rollback()
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_nested_pg_proxy_releases_admission_locks_without_finalizing_outer_run(
    monkeypatch,
) -> None:
    """A hidden embed commits its admission transaction, not the outer run.

    ``NOWAIT`` in a second session is the actual PostgreSQL proof that both
    Task and TaskRun row locks were released before the outer orchestrator
    resumes.  That second session then performs the canonical same-session
    finalization.
    """

    engine = create_engine(
        _DSN,
        pool_pre_ping=True,
        connect_args={"options": "-c lock_timeout=2000ms"},
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    suffix = uuid.uuid4().hex[:12]
    locker = None
    try:
        user = User(
            username=f"gate2-nested-{suffix}",
            role="user",
            hashed_password="synthetic-not-a-secret",
            is_active=True,
            is_approved=True,
        )
        model = ModelRegistry(
            name=f"gate2-nested-model-{suffix}",
            display_name="Gate2 nested PG model",
            model_type="embedding",
            endpoint_url="http://pg-proxy-model:8080",
            is_active=True,
            classification_ceiling="機密",
        )
        db.add_all([user, model])
        db.flush()
        task = Task(
            title="pg nested proxy lock",
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

        async def ignore_usage(**kwargs):
            return None

        monkeypatch.setattr(proxy_service, "_guard_outbound", lambda *a, **k: None)
        monkeypatch.setattr(proxy_service.httpx, "AsyncClient", _PgProxyClient)
        monkeypatch.setattr(
            proxy_service, "enqueue_usage_task_linked", ignore_usage
        )
        result = await proxy_service.proxy_request(
            model=model,
            api_key_id=None,
            user_id=user.id,
            department_id=None,
            request_body={"model": model.name, "input": "nested"},
            endpoint_path="/v1/embeddings",
            task_id=task.id,
            task_trace_id=task.trace_id,
            task_run_id=run.id,
            inference_callsite_id="csp.server_retrieval_embedding",
            governance_db=db,
            admitted_classification_level="機密",
            finalize_task_run_on_completion=False,
        )
        assert result["usage"]["total_tokens"] == 2
        assert not db.in_transaction()

        locker = factory()
        locked_task = (
            locker.query(Task)
            .filter(Task.id == task.id)
            .with_for_update(nowait=True)
            .one()
        )
        locked_run = (
            locker.query(TaskRun)
            .filter(TaskRun.id == run.id)
            .with_for_update(nowait=True)
            .one()
        )
        assert locked_task.status == "running"
        assert locked_run.status == "running"

        finalize_task_run_in_session(locker, run.id, "completed")
        assert not locker.in_transaction()
        db.expire_all()
        assert db.get(Task, task.id).status == "completed"
        assert db.get(TaskRun, run.id).status == "completed"

        db.query(AuditLog).filter_by(
            resource_type="task", resource_id=str(task.id)
        ).delete(synchronize_session=False)
        db.delete(db.get(Task, task.id))
        db.delete(db.get(ModelRegistry, model.id))
        db.delete(db.get(User, user.id))
        db.commit()
    finally:
        if locker is not None:
            locker.rollback()
            locker.close()
        db.rollback()
        db.close()
        engine.dispose()
