"""Durable CSP authority for restart-safe Agent pause/resume.

The session event ledger is immutable history.  These rows are the mutable
control-plane projection used to decide whether a paused run may be resumed.
Only the canonical inner ExecutionGrant is retained; its signed JWT envelope
and every service/agent credential are deliberately excluded.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base

JSONValue = JSON().with_variant(JSONB, "postgresql")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ResumeAuthority(Base):
    """Current verified authority projection for one Agent run."""

    __tablename__ = "resume_authorities"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_resume_authorities_run"),
        # A session may be reused by a later run after the previous one is
        # terminal.  Only one non-terminal binding is allowed at a time.
        Index(
            "uq_resume_authorities_active_session",
            "session_id",
            unique=True,
            postgresql_where=text("lifecycle IN ('blocked', 'resuming', 'paused')"),
            sqlite_where=text("lifecycle IN ('blocked', 'resuming', 'paused')"),
        ),
        CheckConstraint(
            "lifecycle IN ('blocked', 'resuming', 'paused', 'completed', 'failed', 'cancelled')",
            name="ck_resume_authorities_lifecycle",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    caller_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    source_snapshot_id = Column(Integer, nullable=False, index=True)
    trace_id = Column(String(255), nullable=False, index=True)
    invocation_id = Column(String(255), nullable=False, index=True)
    session_id = Column(String(255), nullable=False, index=True)
    agent_db_id = Column(Integer, ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False, index=True)
    agent_id = Column(String(255), nullable=False, index=True)
    classification = Column(String(20), nullable=False)
    registry_snapshot_id = Column(String(255), nullable=False)
    registry_snapshot_revision = Column(String(255), nullable=False)
    registry_snapshot_hash = Column(String(255), nullable=False)
    manifest_revision = Column(String(255), nullable=False)
    manifest_sha256 = Column(String(255), nullable=False)
    grant_id = Column(String(255), nullable=False, index=True)
    route_decision_id = Column(String(255), nullable=False)
    policy_decision_id = Column(String(255), nullable=False)
    auth_session_sid = Column(String(64), nullable=False, index=True)
    model_binding = Column(JSONValue, nullable=False)
    capabilities = Column(JSONValue, nullable=False, default=list)
    scopes = Column(JSONValue, nullable=False, default=list)
    # Canonical, unsigned inner grant only.  Never write the signed JWT token.
    grant_json = Column(JSONValue, nullable=False)
    grant_sha256 = Column(String(64), nullable=False)
    blocked_cursor = Column(Integer, nullable=False)
    lifecycle = Column(String(20), nullable=False, default="blocked", server_default="blocked", index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)
    blocked_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    resumed_at = Column(DateTime(timezone=True), nullable=True)
    terminal_at = Column(DateTime(timezone=True), nullable=True)
    terminal_cursor = Column(Integer, nullable=True)


class ResumeAttempt(Base):
    """Durable exactly-once claim for one blocked cursor."""

    __tablename__ = "resume_attempts"
    __table_args__ = (
        UniqueConstraint("run_id", "blocked_cursor", name="uq_resume_attempts_run_cursor"),
        CheckConstraint(
            "status IN ('claimed', 'paused', 'completed', 'failed', 'cancelled')",
            name="ck_resume_attempts_status",
        ),
        Index("ix_resume_attempts_run_created", "run_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    blocked_cursor = Column(Integer, nullable=False)
    idempotency_key_sha256 = Column(String(64), nullable=False)
    request_sha256 = Column(String(64), nullable=False)
    status = Column(String(20), nullable=False, default="claimed", server_default="claimed", index=True)
    # A claim is a bounded lease, not an eternal process marker.  The raw
    # token is never persisted; its digest plus the monotonic generation form
    # the CSP fencing proof used by completion writers.
    lease_token_sha256 = Column(String(64), nullable=False)
    lease_generation = Column(Integer, nullable=False, default=1, server_default="1")
    lease_expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)
    response_cursor = Column(Integer, nullable=True)
    response_status = Column(String(20), nullable=True)
    # Safe replay metadata only; no Agent payload or bearer material.
    response_meta = Column(JSONValue, nullable=True)


__all__ = ["ResumeAuthority", "ResumeAttempt"]
