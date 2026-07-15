from __future__ import annotations

from app.models.audit_log import AuditLog
from app.models.model_governance_receipt import ModelGovernanceReceipt
from app.models.token_usage import TokenUsage
from app.services.model_governance_receipts import (
    ReceiptSubject,
    SqlAlchemyReceiptSink,
    resolve_model_governance_runtime,
    set_model_governance_runtime_provider,
)

from tests.conftest import make_model, make_user


def _event(invocation_id: str = "inv-durable-1", *, outcome: str = "success") -> dict:
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
        "outcome": outcome,
    }


def test_receipts_are_one_transaction_and_restart_idempotent(db):
    user = make_user(db, username="gate5-receipt-owner")
    model = make_model(db, name="gate5-receipt-model")
    subject = ReceiptSubject(
        user_id=user.id,
        model_id=model.id,
        department_id=user.department_id,
        conversation_id="42",
        actor_username=user.username,
    )
    event = _event()

    first = SqlAlchemyReceiptSink(db, subject=subject)
    pre_usage = first.record_pre_usage(event)
    pre_audit = first.record_pre_audit(event)
    assert pre_usage.startswith("usage:")
    assert pre_audit.startswith("audit:")

    # A fresh sink models process restart/retry. The unique durable ledger,
    # rather than an in-memory map, returns the original receipt ids.
    restarted = SqlAlchemyReceiptSink(db, subject=subject)
    assert restarted.record_pre_usage(event) == pre_usage
    assert restarted.record_pre_audit(event) == pre_audit

    post_event = dict(event)
    post_event.update(
        {
            "phase": "post",
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 5,
                "total_tokens": 8,
                "request_duration_ms": 17,
            },
        }
    )
    post_usage = restarted.record_post_usage(post_event)
    post_audit = restarted.record_post_audit(post_event)
    assert post_usage.startswith("usage:")
    assert post_audit.startswith("audit:")

    # Replaying the post receipt remains exactly-once as well.
    assert restarted.record_post_usage(post_event) == post_usage
    assert restarted.record_post_audit(post_event) == post_audit

    ledger = db.query(ModelGovernanceReceipt).one()
    usage = db.query(TokenUsage).filter(TokenUsage.id == ledger.usage_record_id).one()
    audits = (
        db.query(AuditLog)
        .filter(AuditLog.id.in_([ledger.pre_audit_id, ledger.post_audit_id]))
        .order_by(AuditLog.id)
        .all()
    )
    assert ledger.status == "completed"
    assert usage.prompt_tokens == 3
    assert usage.completion_tokens == 5
    assert usage.total_tokens == 8
    assert usage.request_type == "model_gov"
    assert [row.action for row in audits] == [
        "model.governance.pre",
        "model.governance.post",
    ]


def test_pre_audit_failure_rolls_back_usage_and_writes_failure_audit(db):
    user = make_user(db, username="gate5-receipt-failure")
    model = make_model(db, name="gate5-receipt-failure-model")
    subject = ReceiptSubject(user_id=user.id, model_id=model.id)

    class FailingAuditSink(SqlAlchemyReceiptSink):
        def _audit(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("synthetic audit outage")

    sink = FailingAuditSink(db, subject=subject)
    event = _event("inv-durable-failure")
    sink.record_pre_usage(event)
    try:
        sink.record_pre_audit(event)
    except RuntimeError:
        pass
    else:
        raise AssertionError("synthetic audit failure did not fire")

    # Runtime compensation is represented by the sink's durable failure path.
    sink = SqlAlchemyReceiptSink(db, subject=subject)
    failure_receipt = sink.compensate_pre_usage(event)
    assert failure_receipt.startswith("audit:")
    assert db.query(TokenUsage).filter(TokenUsage.trace_id == "inv-durable-failure").count() == 0
    ledger = (
        db.query(ModelGovernanceReceipt)
        .filter(ModelGovernanceReceipt.invocation_id == "inv-durable-failure")
        .one()
    )
    assert ledger.status == "failed"
    assert (
        db.query(AuditLog)
        .filter(
            AuditLog.id == ledger.post_audit_id,
            AuditLog.status == "failure",
        )
        .count()
        == 1
    )


def test_runtime_provider_is_explicit_and_clear():
    sentinel = object()
    set_model_governance_runtime_provider(lambda: sentinel)  # type: ignore[return-value]
    assert resolve_model_governance_runtime() is sentinel
    set_model_governance_runtime_provider(None)
    assert resolve_model_governance_runtime() is None
