"""Mutation-sensitive PostgreSQL proofs for Gate 2 transaction boundaries.

These tests must run through the non-superuser ``csp_app`` runtime role.  The
admin role is reserved for Alembic setup in CI and cannot make runtime tests
green by bypassing RLS or normal row-lock semantics.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import threading
import time
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from anila_contracts import Classification
from app.models.audit_log import AuditLog
from app.models.clearance import (
    ClearanceGrant,
    ClearanceGrantCompartment,
    CollectionAccessGrant,
    CollectionRequiredCompartment,
    SecurityCompartment,
)
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.models.policy_decision import PolicyDecision
from app.models.source_snapshot import Citation, SourceSnapshot
from app.models.task import Task, TaskRun
from app.models.user import User
from app.modules.clearance.service import resolve_and_evaluate_data_access
from app.modules.policy import apply_classification
from app.services import retrieval_service
from app.services.proxy.task_link import (
    TaskRunContext,
    finalize_task_run_in_session,
    reconcile_stale_task_runs,
)


_DSN = os.environ.get("ANILA_GATE2_LEDGER_PG_URL")
pytestmark = pytest.mark.skipif(
    not _DSN, reason="ANILA_GATE2_LEDGER_PG_URL is not set"
)


def _engine():
    engine = create_engine(
        _DSN,
        pool_pre_ping=True,
        connect_args={"options": "-c lock_timeout=8000ms"},
    )
    with engine.connect() as connection:
        current_user, is_superuser = connection.execute(
            text(
                "SELECT current_user, rolsuper FROM pg_roles "
                "WHERE rolname = current_user"
            )
        ).one()
    assert current_user == "csp_app"
    assert is_superuser is False
    return engine


def _wait_until_lock_blocked(engine, pid: int) -> None:
    deadline = time.monotonic() + 5
    with engine.connect() as connection:
        while time.monotonic() < deadline:
            event = connection.execute(
                text(
                    "SELECT wait_event_type FROM pg_stat_activity "
                    "WHERE pid=:pid"
                ),
                {"pid": pid},
            ).scalar_one_or_none()
            if event == "Lock":
                return
            time.sleep(0.02)
    raise AssertionError(f"backend {pid} never entered a PostgreSQL lock wait")


def _user(suffix: str, label: str, *, role: str = "user") -> User:
    return User(
        username=f"gate2-pg-{label}-{suffix}",
        role=role,
        hashed_password="synthetic-not-a-secret",
        is_active=True,
        is_approved=True,
    )


def test_partial_unique_index_rejects_second_active_run() -> None:
    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    db = factory()
    try:
        user = _user(suffix, "active")
        db.add(user)
        db.flush()
        task = Task(
            title="one active run",
            task_type="query",
            requester_user_id=user.id,
            status="running",
        )
        db.add(task)
        db.flush()
        db.add(
            TaskRun(
                task_id=task.id,
                run_sequence=1,
                dispatch_target="model",
                status="running",
            )
        )
        db.commit()

        db.add(
            TaskRun(
                task_id=task.id,
                run_sequence=2,
                dispatch_target="agent",
                status="queued",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        assert db.query(TaskRun).filter_by(task_id=task.id).count() == 1
    finally:
        db.rollback()
        db.close()
        engine.dispose()


def test_reconciler_skips_locked_task_before_run_and_cannot_deadlock_finalizer() -> None:
    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    seed = factory()
    holder = factory()
    try:
        user = _user(suffix, "lock-order")
        seed.add(user)
        seed.flush()
        task = Task(
            title="lock order",
            task_type="query",
            requester_user_id=user.id,
            status="running",
        )
        seed.add(task)
        seed.flush()
        run = TaskRun(
            task_id=task.id,
            run_sequence=1,
            dispatch_target="model",
            status="running",
            started_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        seed.add(run)
        seed.commit()

        holder.query(Task).filter(Task.id == task.id).with_for_update().one()
        worker_pid: list[int] = []
        entered = threading.Event()

        def reconcile() -> int:
            db = factory()
            try:
                worker_pid.append(
                    int(db.execute(text("SELECT pg_backend_pid()")).scalar_one())
                )
                entered.set()
                return reconcile_stale_task_runs(
                    db, stale_after_seconds=600
                )
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(reconcile)
            assert entered.wait(timeout=5)
            # Parent-first SKIP LOCKED means the reconciler does not acquire
            # the child Run while Task is busy; it returns without waiting.
            assert future.result(timeout=5) >= 0

            # Mutation-sensitive proof: the old Run -> Task reconciler held
            # this row and NOWAIT raised/deadlocked here.
            locked_run = (
                holder.query(TaskRun)
                .filter(TaskRun.id == run.id)
                .with_for_update(nowait=True)
                .one()
            )
            assert locked_run.status == "running"
            assert finalize_task_run_in_session(holder, run.id, "completed")

        verify = factory()
        try:
            assert verify.get(Task, task.id).status == "completed"
            assert verify.get(TaskRun, run.id).status == "completed"
        finally:
            verify.close()
    finally:
        holder.rollback()
        holder.close()
        seed.rollback()
        seed.close()
        engine.dispose()


def test_clearance_compartment_share_lock_blocks_concurrent_revocation() -> None:
    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    seed = factory()
    decision_db = factory()
    try:
        manager = _user(suffix, "manager", role="admin")
        subject = _user(suffix, "subject")
        owner = _user(suffix, "owner")
        seed.add_all([manager, subject, owner])
        seed.flush()
        collection = IngestionCollection(
            name=f"gate2-clearance-{suffix}",
            chunking_config={},
            embedding_model="test-embed",
            embedding_fingerprint="sha256:" + "0" * 64,
            embedding_dim=4000,
            created_by=owner.id,
            classification_level="機密",
        )
        compartment = SecurityCompartment(
            code=f"G2_{suffix.upper()}",
            name="Gate2 barrier",
            created_by_user_id=manager.id,
        )
        seed.add_all([collection, compartment])
        seed.flush()
        now = datetime.now(timezone.utc)
        grant = ClearanceGrant(
            subject_user_id=subject.id,
            max_classification_level="機密",
            valid_from=now - timedelta(hours=1),
            expires_at=now + timedelta(hours=1),
            basis_ticket=f"G2-{suffix}",
            issued_by_user_id=manager.id,
        )
        seed.add(grant)
        seed.flush()
        seed.add_all(
            [
                ClearanceGrantCompartment(
                    clearance_grant_id=grant.id,
                    compartment_id=compartment.id,
                ),
                CollectionRequiredCompartment(
                    collection_id=collection.id,
                    compartment_id=compartment.id,
                    basis_ticket=f"REQ-{suffix}",
                    assigned_by_user_id=manager.id,
                ),
                CollectionAccessGrant(
                    clearance_grant_id=grant.id,
                    collection_id=collection.id,
                    membership_granted=True,
                    need_to_know=True,
                    basis_ticket=f"NTK-{suffix}",
                    issued_by_user_id=manager.id,
                ),
            ]
        )
        seed.commit()

        decision = resolve_and_evaluate_data_access(
            decision_db,
            user_id=subject.id,
            collection_id=collection.id,
            now=now,
        )
        assert decision.allowed is True

        worker_pid: list[int] = []
        entered = threading.Event()

        def revoke_membership() -> None:
            db = factory()
            try:
                worker_pid.append(
                    int(db.execute(text("SELECT pg_backend_pid()")).scalar_one())
                )
                entered.set()
                db.query(ClearanceGrantCompartment).filter_by(
                    clearance_grant_id=grant.id,
                    compartment_id=compartment.id,
                ).delete(synchronize_session=False)
                db.commit()
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(revoke_membership)
            assert entered.wait(timeout=5)
            _wait_until_lock_blocked(engine, worker_pid[0])
            decision_db.commit()
            future.result(timeout=10)

        verify = factory()
        try:
            denied = resolve_and_evaluate_data_access(
                verify,
                user_id=subject.id,
                collection_id=collection.id,
                now=now,
            )
            assert denied.allowed is False
        finally:
            verify.rollback()
            verify.close()
    finally:
        decision_db.rollback()
        decision_db.close()
        seed.rollback()
        seed.close()
        engine.dispose()


class _Chunk:
    def __init__(self, document_id: int) -> None:
        self.id = 730001
        self.document_id = document_id
        self.chunk_key = "gate2:chunk:1"
        self.content = "受控來源內容"
        self.metadata = {"page": 1}
        self.classification_level = "機密"


class _Hit:
    def __init__(self, document_id: int) -> None:
        self.chunk = _Chunk(document_id)
        self.score = 0.91


class _Store:
    document_id: int

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def similarity_search_scoped_documents(self, **kwargs):
        return [_Hit(self.document_id)]

    async def similarity_search_per_document_authorized(self, **kwargs):
        return [_Hit(self.document_id)]


@pytest.mark.asyncio
async def test_retrieval_seal_preserves_concurrent_task_raise_and_commits_ledger(
    monkeypatch, tmp_path: Path
) -> None:
    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    db = factory()
    raiser = factory()
    try:
        user = _user(suffix, "retrieval")
        db.add(user)
        db.flush()
        collection = IngestionCollection(
            name=f"gate2-retrieval-{suffix}",
            chunking_config={},
            embedding_model="test-embed",
            embedding_fingerprint="sha256:" + "0" * 64,
            embedding_dim=3,
            created_by=user.id,
            classification_level="機密",
        )
        db.add(collection)
        db.flush()
        document = IngestionDocument(
            collection_id=collection.id,
            filename="gate2.txt",
            sha256="a" * 64,
            status="indexed",
            classification_level="機密",
        )
        db.add(document)
        db.flush()
        task = Task(
            title="retrieval race",
            task_type="query",
            requester_user_id=user.id,
            status="running",
            source_scope="project",
            selected_collection_ids=[collection.id],
            classification_level="無機密",
        )
        db.add(task)
        db.flush()
        snapshot = SourceSnapshot(
            task_id=task.id,
            origin="collection",
            source_scope="project",
            collection_ids=[collection.id],
            classification_level="無機密",
        )
        db.add(snapshot)
        db.flush()
        task.source_snapshot_id = snapshot.id
        run = TaskRun(
            task_id=task.id,
            run_sequence=1,
            dispatch_target="model",
            status="running",
            classification_level="無機密",
        )
        db.add(run)
        db.commit()
        ctx = TaskRunContext(task.id, task.trace_id, run.id)

        monkeypatch.setattr(
            retrieval_service.settings,
            "SOURCE_SNAPSHOT_STORAGE_PATH",
            str(tmp_path),
        )
        monkeypatch.setattr(
            retrieval_service,
            "embed_query",
            lambda *args, **kwargs: _async_value([0.1, 0.2, 0.3]),
        )
        raised_during_retrieval = False

        def authorize_then_raise(*args, **kwargs):
            nonlocal raised_during_retrieval
            if not raised_during_retrieval:
                apply_classification(
                    raiser,
                    resource_type="task",
                    resource_id=str(task.id),
                    new_level="絕對機密",
                    actor_type="service",
                    actor_id="classification-detector",
                    reason="content_detection",
                    task_id=task.id,
                )
                raised_during_retrieval = True
            return {document.id: Classification.CONFIDENTIAL}

        monkeypatch.setattr(
            retrieval_service,
            "_authorized_retrieval_documents",
            authorize_then_raise,
        )
        _Store.document_id = document.id
        monkeypatch.setattr(
            retrieval_service, "CollectionScopedPgVectorStore", _Store
        )
        monkeypatch.setattr(retrieval_service, "get_pool", lambda: object())

        # Preload stale state. The authorization callback commits a higher
        # classification from another transaction after retrieval begins but
        # before the final Task lock.
        assert db.get(Task, task.id).classification_level == "無機密"

        # Failure injection after Citation staging proves the snapshot,
        # citation, allow decision and audit share one rollback boundary.
        import app.modules.policy as policy_module

        original_record_decision = policy_module.record_decision

        def fail_decision(*args, **kwargs):
            raise RuntimeError("synthetic policy ledger failure")

        monkeypatch.setattr(policy_module, "record_decision", fail_decision)
        with pytest.raises(retrieval_service.RetrievalFailure) as failed:
            await retrieval_service.retrieve_and_seal(
                db,
                user=user,
                task=task,
                collection_id=collection.id,
                query="查詢",
                task_ctx=ctx,
            )
        assert failed.value.code == "snapshot_seal_failed"
        assert not (tmp_path / f"{snapshot.id}.json").exists()

        rolled_back = factory()
        try:
            assert rolled_back.get(SourceSnapshot, snapshot.id).content_hash is None
            assert rolled_back.query(Citation).filter_by(
                source_snapshot_id=snapshot.id
            ).count() == 0
            assert rolled_back.query(PolicyDecision).filter_by(
                task_id=task.id, action="collection.read"
            ).count() == 0
            assert rolled_back.query(AuditLog).filter_by(
                resource_type="task",
                resource_id=str(task.id),
                action="task.policy.allowed",
            ).count() == 0
        finally:
            rolled_back.close()

        monkeypatch.setattr(
            policy_module, "record_decision", original_record_decision
        )
        outcome = await retrieval_service.retrieve_and_seal(
            db,
            user=user,
            task=task,
            collection_id=collection.id,
            query="查詢",
            task_ctx=ctx,
        )
        assert outcome.state == "hits"
        assert raised_during_retrieval is True

        verify = factory()
        try:
            assert verify.get(Task, task.id).classification_level == "絕對機密"
            sealed = verify.get(SourceSnapshot, snapshot.id)
            assert sealed.content_hash == outcome.content_hash
            assert verify.query(Citation).filter_by(
                source_snapshot_id=snapshot.id
            ).count() == 1
            assert verify.query(PolicyDecision).filter_by(
                task_id=task.id,
                action="collection.read",
                decision="allow",
            ).count() == 1
            assert verify.query(AuditLog).filter_by(
                resource_type="task",
                resource_id=str(task.id),
                action="task.policy.allowed",
            ).count() == 1
        finally:
            verify.close()
    finally:
        raiser.rollback()
        raiser.close()
        db.rollback()
        db.close()
        engine.dispose()


async def _async_value(value):
    return value
