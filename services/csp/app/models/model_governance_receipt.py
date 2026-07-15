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
        Index("ix_model_governance_receipts_user_created", "user_id", "created_at"),
        Index("ix_model_governance_receipts_callsite_created", "callsite_id", "created_at"),
    )
