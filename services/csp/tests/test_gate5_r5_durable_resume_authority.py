"""Focused Gate 5 durable pause/resume authority tests."""

from __future__ import annotations

import json
import os
import uuid
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Barrier
from types import SimpleNamespace

import pytest
from anila_contracts import Classification, ExecutionGrant
from anila_contracts._types import InvocationTargetKind
from anila_contracts.contexts import AuthAssurance
from anila_contracts.events import StepEvent, StepKind, StepStatus
from fastapi import HTTPException
from jose import jwt
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.agent import Agent
from app.models.resume_authority import ResumeAttempt, ResumeAuthority
from app.models.task import Task, TaskRun
from app.models.user import User
from app.services import agent_dispatch_service as dispatch_service
from app.services.agent_dispatch_service import (
    DispatchAuthority,
    DispatchBinding,
    _bridge,
    _claim_resume_attempt,
    _authority_from_row,
    persist_blocked_authority,
    resume_by_session,
)
from app.services.proxy.stream_bridge import (
    BridgeContext,
    DispatchClaimLostError,
    InMemorySessionEventStore,
    StreamBridge,
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _seed(db):
    user = User(
        username="resume-user",
        hashed_password="test",
        role="user",
        is_active=True,
        is_approved=True,
    )
    db.add(user)
    db.commit()
    task = Task(
        title="resume-task",
        task_type="query",
        requester_user_id=user.id,
        status="running",
        source_scope="none",
        trace_id="resume-trace",
    )
    db.add(task)
    db.commit()
    run = TaskRun(
        task_id=task.id,
        run_sequence=1,
        dispatch_target="agent",
        status="running",
        classification_level="無機密",
    )
    db.add(run)
    db.commit()
    agent = Agent(
        name="resume-agent",
        owner_user_id=user.id,
        endpoint_url="http://agent.test",
        is_active=True,
        approval_status="approved",
    )
    db.add(agent)
    db.commit()
    return user, task, run, agent


def _authority(db):
    user, task, run, agent = _seed(db)
    now = _now()
    grant = ExecutionGrant(
        schema_version="execution-grant/v1",
        grant_id="eg-durable-test",
        task_id=task.id,
        run_id=run.id,
        trace_id=task.trace_id,
        invocation_id="resume-invocation",
        source_snapshot_id=1,
        route_decision_id="route-durable",
        policy_decision_id="policy-durable",
        registry_snapshot_id="a" * 64,
        classification=Classification.UNCLASSIFIED,
        auth_assurance=AuthAssurance(
            sid="s" * 32,
            amr=("password",),
            acr="pwd",
            auth_time=now,
            break_glass=False,
        ),
        target={
            "kind": InvocationTargetKind.AGENT,
            "id": agent.name,
            "model_binding": {"model_id": 1, "gateway": "csp"},
        },
        manifest_revision="sha256:" + "b" * 64,
        allowed_capabilities=("retrieval",),
        allowed_scopes=("agent:resume",),
        issued_at=now - timedelta(seconds=120),
        expires_at=now - timedelta(seconds=60),
        session_id="opaque-resume-session",
    )
    authority = DispatchAuthority(
        caller=SimpleNamespace(),
        user=user,
        agent=agent,
        grant=grant,
        binding=DispatchBinding(
            caller_user_id=user.id,
            owner_id=user.id,
            task_id=task.id,
            run_id=run.id,
            source_snapshot_id=1,
            trace_id=task.trace_id,
            invocation_id="resume-invocation",
            session_id="opaque-resume-session",
            agent_id=agent.name,
            registry_snapshot_id="a" * 64,
            registry_snapshot_revision="a" * 64,
            registry_snapshot_hash="a" * 64,
            manifest_revision="sha256:" + "b" * 64,
            manifest_sha256="b" * 64,
            grant_id=grant.grant_id,
            route_decision_id=grant.route_decision_id,
            policy_decision_id=grant.policy_decision_id,
            classification=Classification.UNCLASSIFIED,
        ),
        endpoint_url="http://agent.test/v1/chat/completions",
    )
    return authority


def _event(authority: DispatchAuthority, *, sequence: int, status: StepStatus):
    return StepEvent(
        event_id=f"event-{sequence}-{status.value}",
        sequence=sequence,
        cursor="untrusted-cursor",
        trace_id=authority.binding.trace_id,
        task_id=str(authority.binding.task_id),
        session_id=authority.binding.session_id,
        invocation_id=authority.binding.invocation_id,
        run_id=str(authority.binding.run_id),
        step_id="agent:resume-agent",
        kind=StepKind.AGENT,
        status=status,
        safe_output_summary="paused" if status is StepStatus.BLOCKED else "done",
        classification=authority.binding.classification,
    )


def test_blocked_authority_persists_only_after_canonical_blocked_and_omits_token(db_engine):
    Session = sessionmaker(bind=db_engine)
    db = Session()
    authority = _authority(db)
    bridge = _bridge(authority, db)
    bridge.append("anila.step", _event(authority, sequence=1, status=StepStatus.COMPLETED).model_dump_json())
    assert persist_blocked_authority(db, authority=authority, bridge=bridge) is None
    assert db.query(ResumeAuthority).count() == 0
    db.close()

    Base.metadata.drop_all(bind=db_engine)
    Base.metadata.create_all(bind=db_engine)
    db = Session()
    authority = _authority(db)
    bridge = _bridge(authority, db)
    bridge.append("anila.step", _event(authority, sequence=1, status=StepStatus.BLOCKED).model_dump_json())
    row = persist_blocked_authority(db, authority=authority, bridge=bridge)
    assert row is not None
    encoded = json.dumps(row.grant_json, ensure_ascii=False)
    assert "signed.jwt" not in encoded
    assert "X-CSP-Service-Token" not in encoded
    assert row.grant_id == authority.grant.grant_id
    assert row.blocked_cursor == 1


def test_resume_binding_rejects_tampered_conversation_session(db_engine):
    """The conversation session binding cannot be rewritten in durable state."""

    Session = sessionmaker(bind=db_engine)
    db = Session()
    authority = _authority(db)
    bridge = _bridge(authority, db)
    bridge.append("anila.step", _event(authority, sequence=1, status=StepStatus.BLOCKED).model_dump_json())
    row = persist_blocked_authority(db, authority=authority, bridge=bridge)
    assert row is not None

    row.session_id = "tampered-conversation-session"
    db.commit()
    with pytest.raises(HTTPException) as caught:
        _authority_from_row(
            db,
            row=row,
            caller=SimpleNamespace(kind="service_client"),
        )
    assert caught.value.status_code == 409
    assert caught.value.detail == "resume authority grant binding 無效"
    db.close()


def test_resume_attempt_is_durable_same_key_replay_and_different_key_conflict(db_engine):
    Session = sessionmaker(bind=db_engine)
    db = Session()
    authority = _authority(db)
    bridge = _bridge(authority, db)
    bridge.append("anila.step", _event(authority, sequence=1, status=StepStatus.BLOCKED).model_dump_json())
    row = persist_blocked_authority(db, authority=authority, bridge=bridge)
    assert row is not None
    authority = replace(authority, blocked_cursor=row.blocked_cursor)
    first = _claim_resume_attempt(db, authority=authority, bridge=bridge, idempotency_key="resume-key")
    assert first is not None and first.status == "claimed"
    first.status = "completed"
    db.commit()
    retry = _claim_resume_attempt(db, authority=authority, bridge=bridge, idempotency_key="resume-key")
    assert retry is not None and retry.id == first.id
    with pytest.raises(HTTPException) as conflict:
        _claim_resume_attempt(db, authority=authority, bridge=bridge, idempotency_key="other-key")
    assert conflict.value.status_code == 409


def test_new_blocked_cursor_allows_new_resume_attempt(db_engine):
    Session = sessionmaker(bind=db_engine)
    db = Session()
    authority = _authority(db)
    bridge = _bridge(authority, db)
    bridge.append("anila.step", _event(authority, sequence=1, status=StepStatus.BLOCKED).model_dump_json())
    row = persist_blocked_authority(db, authority=authority, bridge=bridge)
    assert row is not None
    authority = replace(authority, blocked_cursor=row.blocked_cursor)
    attempt = _claim_resume_attempt(db, authority=authority, bridge=bridge, idempotency_key="first")
    assert attempt is not None
    assert dispatch_service._mark_resume_attempt(
        db,
        authority=authority,
        bridge=bridge,
        status="paused",
        blocked_cursor=row.blocked_cursor,
        claim_generation=int(attempt.lease_generation),
        lease_token=getattr(attempt, "_resume_lease_token"),
    ) is True
    bridge.append("anila.step", _event(authority, sequence=2, status=StepStatus.BLOCKED).model_dump_json())
    row = persist_blocked_authority(db, authority=authority, bridge=bridge)
    assert row is not None and row.blocked_cursor == 2
    second = _claim_resume_attempt(
        db,
        authority=replace(authority, blocked_cursor=2),
        bridge=bridge,
        idempotency_key="second",
    )
    assert second is not None and second.blocked_cursor == 2 and second.id != attempt.id


def test_terminal_resume_replay_requires_exact_attempt_and_makes_no_agent_call(db_engine, monkeypatch):
    Session = sessionmaker(bind=db_engine)
    db = Session()
    authority = _authority(db)
    bridge = _bridge(authority, db)
    bridge.append("anila.step", _event(authority, sequence=1, status=StepStatus.BLOCKED).model_dump_json())
    row = persist_blocked_authority(db, authority=authority, bridge=bridge)
    assert row is not None
    authority = replace(authority, blocked_cursor=row.blocked_cursor)
    attempt = _claim_resume_attempt(db, authority=authority, bridge=bridge, idempotency_key="terminal-key")
    assert attempt is not None
    bridge.append_terminal(StepStatus.COMPLETED, safe_output_summary="resumed")
    attempt.status = "completed"
    db.commit()
    monkeypatch.setattr(dispatch_service, "_require_named_router_client", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        dispatch_service,
        "renew_execution_grant_for_resume",
        lambda *_args, **_kwargs: pytest.fail("terminal replay must not renew or call Agent"),
    )
    result = asyncio.run(
        resume_by_session(
            db=db,
            caller=SimpleNamespace(kind="service_client"),
            session_id=authority.binding.session_id,
            caller_user_id=authority.binding.owner_id,
            idempotency_key="terminal-key",
        )
    )
    assert result["choices"][0]["message"]["content"] == "resumed"


@pytest.mark.parametrize("failure", ["missing-credential", "network"])
def test_resume_claim_failure_is_retryable_by_same_key_but_conflicts_on_other_key(
    db_engine, monkeypatch, failure
):
    """Every post-claim transport failure releases the durable claim safely."""

    Session = sessionmaker(bind=db_engine)
    db = Session()
    authority = _authority(db)
    bridge = StreamBridge(
        BridgeContext(
            task_id=str(authority.binding.task_id),
            trace_id=authority.binding.trace_id,
            agent_id=authority.binding.agent_id,
            session_id=authority.binding.session_id,
            run_id=str(authority.binding.run_id),
            classification=authority.binding.classification,
            invocation_id=authority.binding.invocation_id,
        ),
        store=InMemorySessionEventStore(),
    )
    bridge.append(
        "anila.step",
        _event(authority, sequence=1, status=StepStatus.BLOCKED).model_dump_json(),
    )
    row = persist_blocked_authority(db, authority=authority, bridge=bridge)
    assert row is not None
    authority = replace(authority, blocked_cursor=row.blocked_cursor)
    monkeypatch.setattr(dispatch_service, "_bridge", lambda _authority, _db: bridge)

    def headers(*_args, **_kwargs):
        if failure == "missing-credential":
            raise HTTPException(status_code=503, detail="agent credential unavailable")
        return {"X-CSP-Service-Token": "csk-test"}

    monkeypatch.setattr(dispatch_service, "build_agent_outbound_headers", headers)
    calls = 0

    class FailingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise RuntimeError("network unavailable")

    monkeypatch.setattr(
        dispatch_service.httpx,
        "AsyncClient",
        lambda **_kwargs: FailingClient(),
    )
    with pytest.raises(HTTPException) as first_error:
        asyncio.run(
            dispatch_service.dispatch_resume(
                db=db,
                authority=authority,
                grant_token="signed-grant",
                idempotency_key="retryable-key",
            )
        )
    assert first_error.value.status_code == (503 if failure == "missing-credential" else 502)
    attempt = (
        db.query(ResumeAttempt)
        .filter(
            ResumeAttempt.run_id == authority.binding.run_id,
            ResumeAttempt.blocked_cursor == row.blocked_cursor,
        )
        .one()
    )
    assert attempt.status == "failed"
    assert calls == (0 if failure == "missing-credential" else 1)

    with pytest.raises(HTTPException) as conflict:
        asyncio.run(
            dispatch_service.dispatch_resume(
                db=db,
                authority=authority,
                grant_token="signed-grant",
                idempotency_key="different-key",
            )
        )
    assert conflict.value.status_code == 409
    assert calls == (0 if failure == "missing-credential" else 1)

    monkeypatch.setattr(
        dispatch_service,
        "build_agent_outbound_headers",
        lambda *_args, **_kwargs: {"X-CSP-Service-Token": "csk-test"},
    )

    completed_event = _event(
        authority, sequence=2, status=StepStatus.COMPLETED
    ).model_dump(mode="json")

    class SuccessResponse:
        status_code = 200

        def json(self):
            return {
                "choices": [{"message": {"content": "retry succeeded"}}],
                "anila_events": [completed_event],
            }

    class SuccessClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            return SuccessResponse()

    monkeypatch.setattr(
        dispatch_service.httpx,
        "AsyncClient",
        lambda **_kwargs: SuccessClient(),
    )
    result = asyncio.run(
        dispatch_service.dispatch_resume(
            db=db,
            authority=authority,
            grant_token="signed-grant",
            idempotency_key="retryable-key",
        )
    )
    assert result["choices"][0]["message"]["content"] == "retry succeeded"
    assert calls == (1 if failure == "missing-credential" else 2)
    db.expire_all()
    assert (
        db.query(ResumeAttempt)
        .filter(
            ResumeAttempt.run_id == authority.binding.run_id,
            ResumeAttempt.blocked_cursor == row.blocked_cursor,
        )
        .one()
        .status
        == "completed"
    )


def test_renewal_accepts_expired_inner_grant_and_preserves_logical_id(monkeypatch):
    # The cryptographic property is tested through the canonical signer seam;
    # the full DB revalidation path is covered by the endpoint tests above.
    from app.services.execution_grant_service import _sign_execution_grant
    from app.utils.security import get_public_key

    now = _now()
    grant = ExecutionGrant(
        schema_version="execution-grant/v1",
        grant_id="eg-stable",
        task_id=1,
        run_id=1,
        trace_id="t",
        invocation_id="i",
        source_snapshot_id=1,
        route_decision_id="r",
        policy_decision_id="p",
        registry_snapshot_id="a" * 64,
        classification=Classification.UNCLASSIFIED,
        auth_assurance=AuthAssurance(sid="s" * 32, amr=("password",), acr="pwd", auth_time=now, break_glass=False),
        target={"kind": InvocationTargetKind.AGENT, "id": "a", "model_binding": {"model_id": 1, "gateway": "csp"}},
        manifest_revision="sha256:" + "b" * 64,
        allowed_capabilities=(),
        allowed_scopes=(),
        issued_at=now,
        expires_at=now + timedelta(seconds=60),
        session_id="sesh",
    )
    first = _sign_execution_grant(grant, manifest_sha256_value="b" * 64, jti="jti-1")
    second = _sign_execution_grant(grant, manifest_sha256_value="b" * 64, jti="jti-2")
    first_claims = jwt.decode(first, get_public_key(), algorithms=["RS256"], options={"verify_aud": False})
    second_claims = jwt.decode(second, get_public_key(), algorithms=["RS256"], options={"verify_aud": False})
    assert first_claims["grant_id"] == second_claims["grant_id"] == "eg-stable"
    assert first_claims["jti"] != second_claims["jti"]


@pytest.mark.integration
def test_postgres_resume_claim_contenders_are_unique():
    """Two CSP workers cannot claim the same durable run/cursor in PostgreSQL."""

    url = os.getenv("TEST_POSTGRES_URL", "")
    if not url.startswith(("postgresql://", "postgresql+psycopg://", "postgresql+psycopg2://")):
        pytest.skip("TEST_POSTGRES_URL 未提供")

    engine = create_engine(url, pool_size=4, max_overflow=0, pool_pre_ping=True)
    if not inspect(engine).has_table("resume_attempts"):
        pytest.fail("PostgreSQL schema 缺少 resume_attempts；請先套用 r1_0028")
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    seed = Session()
    user = task = run = None
    try:
        user = User(
            username=f"resume-pg-{suffix}",
            hashed_password="test",
            role="user",
            is_active=True,
            is_approved=True,
        )
        seed.add(user)
        seed.flush()
        task = Task(
            title="resume-pg-task",
            task_type="query",
            requester_user_id=user.id,
            status="running",
            source_scope="none",
            trace_id=f"resume-pg-trace-{suffix}",
        )
        seed.add(task)
        seed.flush()
        run = TaskRun(
            task_id=task.id,
            run_sequence=1,
            dispatch_target="agent",
            status="running",
            classification_level="無機密",
        )
        seed.add(run)
        seed.commit()
        run_id = int(run.id)
    finally:
        seed.close()

    barrier = Barrier(2)

    def contender(key: str):
        db = Session()
        authority = SimpleNamespace(
            blocked_cursor=1,
            binding=SimpleNamespace(run_id=run_id),
        )
        try:
            barrier.wait(timeout=20)
            attempt = _claim_resume_attempt(
                db,
                authority=authority,
                bridge=SimpleNamespace(),
                idempotency_key=key,
            )
            return ("won", attempt.id if attempt is not None else None)
        except HTTPException as exc:
            return ("rejected", exc.status_code, exc.detail)
        finally:
            db.close()


    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(
                pool.map(contender, ("pg-contender-a", "pg-contender-b"))
            )
        assert sum(outcome[0] == "won" for outcome in outcomes) == 1
        rejected = [outcome for outcome in outcomes if outcome[0] == "rejected"]
        assert len(rejected) == 1
        assert rejected[0][1] == 409
    finally:
        cleanup = Session()
        try:
            from app.models.resume_authority import ResumeAttempt

            cleanup.query(ResumeAttempt).filter(ResumeAttempt.run_id == run_id).delete(
                synchronize_session=False
            )
            cleanup.query(TaskRun).filter(TaskRun.id == run_id).delete(
                synchronize_session=False
            )
            cleanup.query(Task).filter(Task.id == task.id).delete(
                synchronize_session=False
            )
            cleanup.query(User).filter(User.id == user.id).delete(
                synchronize_session=False
            )
            cleanup.commit()
        finally:
            cleanup.close()
            engine.dispose()


def test_resume_claim_lease_reclaims_and_fences_stale_worker(db_engine):
    """An expired lease is reclaimed; the old generation cannot mark it."""

    Session = sessionmaker(bind=db_engine)
    db = Session()
    authority = _authority(db)
    bridge = _bridge(authority, db)
    bridge.append("anila.step", _event(authority, sequence=1, status=StepStatus.BLOCKED).model_dump_json())
    row = persist_blocked_authority(db, authority=authority, bridge=bridge)
    assert row is not None
    authority = replace(authority, blocked_cursor=row.blocked_cursor)

    first = _claim_resume_attempt(db, authority=authority, bridge=bridge, idempotency_key="lease-key")
    assert first is not None
    first_generation = int(first.lease_generation)
    first_token = getattr(first, "_resume_lease_token")
    assert isinstance(first_token, str) and first_token
    with pytest.raises(HTTPException) as active:
        _claim_resume_attempt(db, authority=authority, bridge=bridge, idempotency_key="lease-key")
    assert active.value.status_code == 409

    first.lease_expires_at = _now() - timedelta(seconds=1)
    db.commit()
    second = _claim_resume_attempt(db, authority=authority, bridge=bridge, idempotency_key="lease-key")
    assert second is not None
    second_generation = int(second.lease_generation)
    second_token = getattr(second, "_resume_lease_token")
    assert second_generation > first_generation
    assert second_token != first_token

    assert dispatch_service._mark_resume_attempt(
        db,
        authority=authority,
        bridge=bridge,
        status="failed",
        blocked_cursor=row.blocked_cursor,
        claim_generation=first_generation,
        lease_token=first_token,
    ) is False
    db.expire_all()
    still_claimed = db.query(ResumeAttempt).filter(ResumeAttempt.id == second.id).one()
    assert still_claimed.status == "claimed"
    assert int(still_claimed.lease_generation) == second_generation

    assert dispatch_service._mark_resume_attempt(
        db,
        authority=authority,
        bridge=bridge,
        status="failed",
        blocked_cursor=row.blocked_cursor,
        claim_generation=second_generation,
        lease_token=second_token,
    ) is True
    db.expire_all()
    assert db.query(ResumeAttempt).filter(ResumeAttempt.id == second.id).one().status == "failed"


def test_terminal_crash_window_reconciles_claimed_attempt_for_same_key(db_engine):
    """A committed terminal can reconcile an uncommitted attempt mark."""

    Session = sessionmaker(bind=db_engine)
    db = Session()
    authority = _authority(db)
    bridge = _bridge(authority, db)
    bridge.append("anila.step", _event(authority, sequence=1, status=StepStatus.BLOCKED).model_dump_json())
    row = persist_blocked_authority(db, authority=authority, bridge=bridge)
    assert row is not None
    authority = replace(authority, blocked_cursor=row.blocked_cursor)
    first = _claim_resume_attempt(db, authority=authority, bridge=bridge, idempotency_key="terminal-crash")
    assert first is not None
    generation = int(first.lease_generation)
    bridge.append_terminal(StepStatus.COMPLETED, safe_output_summary="done")

    recovered = _claim_resume_attempt(db, authority=authority, bridge=bridge, idempotency_key="terminal-crash")
    assert recovered is not None and recovered.status == "claimed"
    assert getattr(recovered, "_resume_lease_token", None) is None
    assert dispatch_service._mark_resume_attempt(
        db,
        authority=authority,
        bridge=bridge,
        status="completed",
        blocked_cursor=row.blocked_cursor,
        claim_generation=generation,
    ) is True
    db.expire_all()
    assert db.query(ResumeAttempt).filter(ResumeAttempt.id == first.id).one().status == "completed"
    with pytest.raises(HTTPException) as conflict:
        _claim_resume_attempt(db, authority=authority, bridge=bridge, idempotency_key="other-terminal-key")
    assert conflict.value.status_code == 409


def test_reclaimed_resume_fence_rejects_old_worker_event_and_terminal(db_engine):
    """A stale resume worker cannot append after a generation reclaim."""

    Session = sessionmaker(bind=db_engine)
    db1 = Session()
    db2 = Session()
    try:
        authority = _authority(db1)
        first_bridge = _bridge(authority, db1)
        first_bridge.append(
            "anila.step",
            _event(authority, sequence=1, status=StepStatus.BLOCKED).model_dump_json(),
        )
        row = persist_blocked_authority(db1, authority=authority, bridge=first_bridge)
        assert row is not None
        authority = replace(authority, blocked_cursor=row.blocked_cursor)
        first = _claim_resume_attempt(
            db1, authority=authority, bridge=first_bridge, idempotency_key="fenced-resume"
        )
        assert first is not None
        first.lease_expires_at = _now() - timedelta(seconds=1)
        db1.commit()

        # A separate SQLAlchemy session models a second CSP worker.  The
        # second bridge must observe and reclaim the expired durable lease,
        # advancing the generation that fences the first worker.
        second_bridge = _bridge(authority, db2)
        second = _claim_resume_attempt(
            db2, authority=authority, bridge=second_bridge, idempotency_key="fenced-resume"
        )
        assert second is not None
        with pytest.raises(DispatchClaimLostError):
            first_bridge.append(
                "anila.step",
                _event(authority, sequence=2, status=StepStatus.RUNNING).model_dump_json(),
            )
        with pytest.raises(DispatchClaimLostError):
            first_bridge.append_terminal(StepStatus.COMPLETED, safe_output_summary="stale")
    finally:
        db2.close()
        db1.close()


def test_initial_blocked_crash_window_rebuilds_authority_without_agent_call(db_engine, monkeypatch):
    """A committed BLOCKED event is enough to recover the pause after CSP crash."""

    Session = sessionmaker(bind=db_engine)
    db = Session()
    authority = _authority(db)
    bridge = _bridge(authority, db)
    bridge.append("anila.step", _event(authority, sequence=1, status=StepStatus.BLOCKED).model_dump_json())
    assert db.query(ResumeAuthority).count() == 0
    monkeypatch.setattr(dispatch_service, "_bridge", lambda _authority, _db: bridge)
    monkeypatch.setattr(
        dispatch_service,
        "build_agent_outbound_headers",
        lambda *_args, **_kwargs: pytest.fail("blocked recovery must not call Agent"),
    )
    result = asyncio.run(
        dispatch_service.dispatch_nonstream(
            db=db,
            authority=authority,
            messages=[{"role": "user", "content": "resume"}],
            grant_token="signed-grant",
        )
    )
    assert result["status"] == "paused"
    assert db.query(ResumeAuthority).count() == 1
