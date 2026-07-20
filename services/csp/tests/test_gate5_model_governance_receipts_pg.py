"""PostgreSQL race proofs for the Gate 5 durable receipt ledger."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.audit_log import AuditLog
from app.models.model_governance_receipt import ModelGovernanceReceipt
from app.models.model_registry import ModelRegistry
from app.models.token_usage import TokenUsage
from app.models.user import User
from app.services.model_governance_receipts import ReceiptSubject, SqlAlchemyReceiptSink
from app.utils.security import hash_password


PG_URL = os.environ.get("ANILA_GATE2_LEDGER_PG_URL")
pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="ANILA_GATE2_LEDGER_PG_URL is not set",
)


@pytest.fixture(scope="module")
def pg_session_factory():
    assert PG_URL
    engine = create_engine(PG_URL, pool_size=8, max_overflow=4, pool_pre_ping=True)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def _subject(factory):
    db = factory()
    suffix = uuid4().hex
    user = User(
        username=f"gate5-pg-{suffix}",
        hashed_password=hash_password("password"),
        role="user",
        is_active=True,
        is_approved=True,
    )
    model = ModelRegistry(
        name=f"gate5-pg-model-{suffix}",
        display_name="Gate5 PG model",
        model_type="llm",
        endpoint_url="http://unused-gate5-pg.invalid/v1",
        is_active=True,
    )
    db.add_all([user, model])
    db.commit()
    subject = ReceiptSubject(user_id=user.id, model_id=model.id)
    db.close()
    return subject


def _event(invocation_id: str) -> dict:
    return {
        "schema_version": "anila.gate5.model-governance.receipt.v1",
        "phase": "pre",
        "invocation_id": invocation_id,
        "callsite_id": "r7.csp.memory",
        "classification": "機密",
        "gateway_id": "csp-model-gateway",
        "endpoint": "https://csp-model-gateway/v1",
        "artifact_id": "artifact.synthetic",
        "deployment_id": "deployment.synthetic",
    }


def test_pg_pre_post_receipts_commit_together(pg_session_factory):
    subject = _subject(pg_session_factory)
    event = _event(f"pg-pre-post-{uuid4().hex}")
    db = pg_session_factory()
    sink = SqlAlchemyReceiptSink(db, subject=subject)
    pre_usage = sink.record_pre_usage(event)
    pre_audit = sink.record_pre_audit(event)
    post = dict(
        event,
        phase="post",
        pre_usage_receipt=pre_usage,
        pre_audit_receipt=pre_audit,
        usage={"prompt_tokens": 2, "completion_tokens": 4},
    )
    sink.record_post_usage(post)
    sink.record_post_audit(post)

    ledger = (
        db.query(ModelGovernanceReceipt)
        .filter(ModelGovernanceReceipt.invocation_id == event["invocation_id"])
        .one()
    )
    usage = db.get(TokenUsage, ledger.usage_record_id)
    audits = (
        db.query(AuditLog)
        .filter(AuditLog.id.in_([ledger.pre_audit_id, ledger.post_audit_id]))
        .order_by(AuditLog.id)
        .all()
    )
    assert ledger.status == "completed"
    assert usage is not None and usage.total_tokens == 6
    assert [row.status for row in audits] == ["started", "success"]
    db.close()


def test_pg_restart_retry_rejects_unique_invocation_ledger(pg_session_factory):
    subject = _subject(pg_session_factory)
    event = _event(f"pg-retry-{uuid4().hex}")
    first = pg_session_factory()
    sink = SqlAlchemyReceiptSink(first, subject=subject)
    sink.record_pre_usage(event)
    sink.record_pre_audit(event)
    first.close()

    restarted = pg_session_factory()
    retry = SqlAlchemyReceiptSink(restarted, subject=subject)
    with pytest.raises(RuntimeError, match="already exists|new invocation_id"):
        retry.record_pre_usage(event)
    assert (
        restarted.query(ModelGovernanceReceipt)
        .filter(ModelGovernanceReceipt.invocation_id == event["invocation_id"])
        .count()
        == 1
    )
    assert (
        restarted.query(TokenUsage)
        .filter(TokenUsage.trace_id == event["invocation_id"])
        .count()
        == 1
    )
    restarted.close()


def test_pg_concurrent_same_invocation_has_one_authorization(pg_session_factory):
    subject = _subject(pg_session_factory)
    event = _event(f"pg-race-{uuid4().hex}")

    def worker() -> tuple[str, str] | RuntimeError:
        db = pg_session_factory()
        try:
            sink = SqlAlchemyReceiptSink(db, subject=subject)
            try:
                return sink.record_pre_usage(event), sink.record_pre_audit(event)
            except RuntimeError as exc:
                return exc
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))

    successes = [item for item in results if isinstance(item, tuple)]
    failures = [item for item in results if isinstance(item, RuntimeError)]
    assert len(successes) == 1
    assert len(failures) == 1
    db = pg_session_factory()
    try:
        assert (
            db.query(ModelGovernanceReceipt)
            .filter(ModelGovernanceReceipt.invocation_id == event["invocation_id"])
            .count()
            == 1
        )
        assert (
            db.query(TokenUsage)
            .filter(TokenUsage.trace_id == event["invocation_id"])
            .count()
            == 1
        )
        ledger = (
            db.query(ModelGovernanceReceipt)
            .filter(ModelGovernanceReceipt.invocation_id == event["invocation_id"])
            .one()
        )
        assert (
            db.query(AuditLog)
            .filter(AuditLog.id == ledger.pre_audit_id)
            .count()
            == 1
        )
    finally:
        db.close()
