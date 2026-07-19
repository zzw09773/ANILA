"""Durable exactly-once ledger for Gate 5 model invocation receipts."""

from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from app.database import Base


class ModelGovernanceReceipt(Base):
    __tablename__ = "model_governance_receipts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # One durable ledger row per invocation.  This is the idempotency key used
    # by retries after a process restart; it is intentionally stronger than a
    # process-local dictionary.
    invocation_id = Column(String(255), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    model_id = Column(Integer, ForeignKey("model_registry.id"), nullable=True)
    callsite_id = Column(String(128), nullable=False)
    provider_binding_id = Column(String(128), nullable=True)
    provider_locality = Column(String(32), nullable=True)
    transport_target_sha256 = Column(String(64), nullable=True)
    model_registry_revision = Column(String(256), nullable=True)
    upstream_provider_locality = Column(String(32), nullable=True)
    upstream_transport_target_sha256 = Column(String(64), nullable=True)
    egress_policy_id = Column(String(128), nullable=True)
    upstream_egress_policy_id = Column(String(128), nullable=True)
    profile_content_sha256 = Column(String(64), nullable=True)
    inventory_sha256 = Column(String(64), nullable=True)
    status = Column(String(20), nullable=False, default="pre")
    usage_record_id = Column(
        Integer,
        ForeignKey("token_usage.id", ondelete="SET NULL"),
        nullable=True,
    )
    pre_audit_id = Column(
        Integer,
        ForeignKey("audit_logs.id", ondelete="SET NULL"),
        nullable=True,
    )
    post_audit_id = Column(
        Integer,
        ForeignKey("audit_logs.id", ondelete="SET NULL"),
        nullable=True,
    )
    metadata_json = Column(Text, nullable=True)
    created_at = Column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    __table_args__ = (
        UniqueConstraint(
            "invocation_id", name="uq_model_governance_receipts_invocation"
        ),
        CheckConstraint(
            "status IN ('pre', 'authorized', 'completed', 'failed')",
            name="ck_model_governance_receipts_status",
        ),
        CheckConstraint(
            "(provider_binding_id IS NULL AND provider_locality IS NULL "
            "AND transport_target_sha256 IS NULL "
            "AND model_registry_revision IS NULL "
            "AND upstream_provider_locality IS NULL "
            "AND upstream_transport_target_sha256 IS NULL "
            "AND egress_policy_id IS NULL AND upstream_egress_policy_id IS NULL) OR "
            "(provider_binding_id IS NOT NULL AND provider_locality IS NOT NULL "
            "AND transport_target_sha256 IS NOT NULL "
            "AND model_registry_revision IS NOT NULL "
            "AND profile_content_sha256 IS NOT NULL AND inventory_sha256 IS NOT NULL "
            "AND ((upstream_provider_locality IS NULL "
            "AND upstream_transport_target_sha256 IS NULL) OR "
            "(upstream_provider_locality IS NOT NULL "
            "AND upstream_transport_target_sha256 IS NOT NULL)))",
            name="ck_model_governance_receipts_provider_snapshot",
        ),
        Index("ix_model_governance_receipts_user_created", "user_id", "created_at"),
        Index("ix_model_governance_receipts_callsite_created", "callsite_id", "created_at"),
    )
