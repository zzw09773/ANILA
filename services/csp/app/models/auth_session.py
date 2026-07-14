"""Durable browser/SDK auth sessions and one-time refresh-token families."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    sid = Column(String(64), primary_key=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    refresh_family_id = Column(String(64), nullable=False, unique=True)
    amr_json = Column(Text, nullable=False, default="[]")
    acr = Column(String(128), nullable=False)
    auth_time = Column(DateTime(timezone=True), nullable=False)
    break_glass = Column(Boolean, nullable=False, default=False)
    # Bind a privileged password session to the incident window that minted it.
    # These are deliberately durable (not only JWT claims), so opening a later
    # break-glass window cannot revive an ordinary or earlier password session.
    break_glass_ticket = Column(String(128), nullable=True)
    break_glass_expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    revoked_at = Column(DateTime(timezone=True), nullable=True, index=True)
    revoke_reason = Column(String(128), nullable=True)

    __table_args__ = (
        CheckConstraint("length(sid) >= 32", name="ck_auth_sessions_sid_entropy"),
        CheckConstraint(
            "length(refresh_family_id) >= 32",
            name="ck_auth_sessions_family_entropy",
        ),
        CheckConstraint("length(acr) > 0", name="ck_auth_sessions_acr_nonempty"),
        CheckConstraint(
            "(break_glass = false AND break_glass_ticket IS NULL "
            "AND break_glass_expires_at IS NULL) OR "
            "(break_glass = true AND break_glass_ticket IS NOT NULL "
            "AND break_glass_expires_at IS NOT NULL)",
            name="ck_auth_sessions_break_glass_binding",
        ),
        Index("ix_auth_sessions_user_active", "user_id", "revoked_at"),
    )


class AuthRefreshToken(Base):
    __tablename__ = "auth_refresh_tokens"

    # Only a SHA-256 digest is durable; bearer material and raw JTI values are
    # never written to the database or audit log.
    jti_hash = Column(String(64), primary_key=True)
    sid = Column(
        String(64),
        ForeignKey("auth_sessions.sid", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    generation = Column(Integer, nullable=False)
    parent_jti_hash = Column(String(64), nullable=True)
    issued_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "length(jti_hash) = 64",
            name="ck_auth_refresh_jti_hash_length",
        ),
        CheckConstraint(
            "parent_jti_hash IS NULL OR length(parent_jti_hash) = 64",
            name="ck_auth_refresh_parent_hash_length",
        ),
        CheckConstraint(
            "generation >= 0",
            name="ck_auth_refresh_generation_nonnegative",
        ),
        CheckConstraint(
            "expires_at > issued_at",
            name="ck_auth_refresh_expiry_order",
        ),
        UniqueConstraint("sid", "generation", name="uq_auth_refresh_sid_generation"),
    )
