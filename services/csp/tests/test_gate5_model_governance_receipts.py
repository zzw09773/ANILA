from __future__ import annotations

import json

import pytest
from sqlalchemy.exc import IntegrityError

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
        "provider_binding_id": "provider.synthetic",
        "provider_locality": "internal_isolated",
        "transport_target": "model.internal:8000",
        "transport_target_sha256": "a" * 64,
        "model_registry_revision": "registry-rev-1",
        "upstream_provider_locality": None,
        "upstream_transport_target": None,
        "upstream_transport_target_sha256": None,
        "egress_policy_id": None,
        "upstream_egress_policy_id": None,
        "profile_content_sha256": "b" * 64,
        "inventory_sha256": "c" * 64,
        "outcome": outcome,
    }


def test_receipts_are_one_transaction_and_replay_is_fail_closed(db):
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

    # A fresh sink models process restart/retry. Reusing the durable
    # invocation id is denied before any second network authorization.
    restarted = SqlAlchemyReceiptSink(db, subject=subject)
    with pytest.raises(RuntimeError, match="already exists|new invocation_id"):
        restarted.record_pre_usage(event)

    post_event = dict(event)
    post_event.update(
        {
            "phase": "post",
            "pre_usage_receipt": pre_usage,
            "pre_audit_receipt": pre_audit,
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 5,
                "total_tokens": 8,
                "request_duration_ms": 17,
            },
        }
    )
    post_usage = first.record_post_usage(post_event)
    post_audit = first.record_post_audit(post_event)
    assert post_usage.startswith("usage:")
    assert post_audit.startswith("audit:")

    # A post receipt is closed too; a fresh sink cannot replay it.
    with pytest.raises(RuntimeError, match="already exists|new invocation_id"):
        restarted.record_post_usage(post_event)

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
    replay = SqlAlchemyReceiptSink(db, subject=subject)
    with pytest.raises(RuntimeError, match="already exists|new invocation_id"):
        replay.record_pre_usage(event)


def test_receipt_metadata_excludes_raw_provider_targets(db):
    user = make_user(db, username="gate5-receipt-redaction")
    model = make_model(db, name="gate5-receipt-redaction-model")
    sink = SqlAlchemyReceiptSink(
        db,
        subject=ReceiptSubject(user_id=user.id, model_id=model.id),
    )
    event = _event("inv-redaction")
    sink.record_pre_usage(event)
    sink.record_pre_audit(event)

    ledger = db.query(ModelGovernanceReceipt).one()
    pre_audit = db.query(AuditLog).filter(AuditLog.id == ledger.pre_audit_id).one()
    for payload in (ledger.metadata_json, pre_audit.metadata_json):
        assert payload is not None
        encoded = json.loads(payload)
        assert "transport_target" not in encoded
        assert "upstream_transport_target" not in encoded
    assert not hasattr(ModelGovernanceReceipt, "transport_target")
    assert not hasattr(ModelGovernanceReceipt, "upstream_transport_target")


def test_runtime_provider_is_explicit_and_clear():
    sentinel = object()
    set_model_governance_runtime_provider(lambda: sentinel)  # type: ignore[return-value]
    assert resolve_model_governance_runtime() is sentinel
    set_model_governance_runtime_provider(None)
    assert resolve_model_governance_runtime() is None


def test_receipt_replay_rejects_provider_or_profile_drift(db):
    user = make_user(db, username="gate5-receipt-drift")
    model = make_model(db, name="gate5-receipt-drift-model")
    sink = SqlAlchemyReceiptSink(
        db,
        subject=ReceiptSubject(user_id=user.id, model_id=model.id),
    )
    event = _event("inv-provider-drift")
    sink.record_pre_usage(event)
    sink.record_pre_audit(event)

    for field, value in (
        ("provider_binding_id", "provider.other"),
        ("provider_locality", "external_governed"),
        ("transport_target", "other.internal:8000"),
        ("transport_target_sha256", "e" * 64),
        ("model_registry_revision", "registry-rev-2"),
        ("upstream_provider_locality", "external_governed"),
        ("upstream_transport_target", "upstream.example:443"),
        ("upstream_transport_target_sha256", "f" * 64),
        ("egress_policy_id", "egress.other"),
        ("upstream_egress_policy_id", "egress.upstream.other"),
        ("profile_content_sha256", "d" * 64),
        ("inventory_sha256", "9" * 64),
    ):
        replay = dict(event)
        replay[field] = value
        with pytest.raises(RuntimeError, match="replay drift|already exists"):
            sink.record_pre_usage(replay)


def test_receipt_provider_snapshot_is_all_legacy_null_or_complete(db):
    user = make_user(db, username="gate5-receipt-constraint")
    model = make_model(db, name="gate5-receipt-constraint-model")
    db.add(
        ModelGovernanceReceipt(
            invocation_id="inv-partial-provider-snapshot",
            user_id=user.id,
            model_id=model.id,
            callsite_id="r7.csp.proxy",
            provider_binding_id="provider.partial",
            status="pre",
        )
    )

    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()

    # A newly emitted v1 receipt can still carry signed inventory/profile
    # hashes while having no provider-binding extension at all.
    db.add(
        ModelGovernanceReceipt(
            invocation_id="inv-v1-with-authority-hashes",
            user_id=user.id,
            model_id=model.id,
            callsite_id="r7.csp.proxy",
            profile_content_sha256="a" * 64,
            inventory_sha256="b" * 64,
            status="pre",
        )
    )
    db.flush()
