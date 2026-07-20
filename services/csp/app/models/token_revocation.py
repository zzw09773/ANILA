"""Durable log of JWT token-revocation events.

Rows record either a whole-user ``token_version`` bump or a focused digest of
one JWT ``jti`` / auth-session ``sid``. The
table backs the cold-start sync endpoint
``GET /api/auth/revocations?since=...`` that downstream services
(anila-studio etc.) hit on boot before subscribing to the live
``anila:auth:token-revoke`` Redis channel.

Why a separate table instead of mining ``audit_log``:

* Audit log fields are generic / free-text — the consumer would
  have to parse Chinese descriptions. A dedicated table gives us
  typed columns + an index on ``revoked_at`` for fast since-filter
  queries.
* Retention: the audit log is kept forever for compliance, but we
  only need ~30 days of revocations (matches the access-token
  lifetime + safety margin). Cleanup can run independently.

The composite index on ``(user_id, revoked_at)`` lets the future
"give me the latest revocation for user X" lookup stay fast even on
a fleet with millions of historical revocations.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)

from app.database import Base


class TokenRevocation(Base):
    __tablename__ = "token_revocations"

    # ``BigInteger`` PK on Postgres (production), plain ``Integer`` on
    # SQLite (tests). Pure-BigInteger autoincrement doesn't work on
    # SQLite — only ``INTEGER PRIMARY KEY`` triggers its rowid auto-
    # increment. ``with_variant`` keeps the schema honest without
    # forcing a custom DDL path in the migration.
    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # ``users.token_version`` value AFTER the bump. Subscribers reject
    # any JWT whose ``tv`` claim is < this value for that user.
    revoked_at_version = Column(Integer, nullable=False)
    # ``user_version`` preserves the original whole-user contract. ``jti``
    # and ``sid`` add focused revocation without forcing unrelated sessions
    # offline. Raw identifiers are never stored; only SHA-256 digests cross
    # the durable/audit boundary.
    scope = Column(String(16), nullable=False, default="user_version", index=True)
    token_jti_hash = Column(String(64), nullable=True, index=True)
    session_id_hash = Column(String(64), nullable=True, index=True)
    token_type = Column(String(16), nullable=True)
    reason = Column(String(128), nullable=True)
    revoked_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    __table_args__ = (
        CheckConstraint(
            "scope IN ('user_version', 'jti', 'sid')",
            name="ck_token_revocations_scope",
        ),
        CheckConstraint(
            "(scope = 'user_version' AND token_jti_hash IS NULL "
            "AND session_id_hash IS NULL AND token_type IS NULL) OR "
            "(scope = 'jti' AND token_jti_hash IS NOT NULL "
            "AND session_id_hash IS NULL "
            "AND token_type IN ('access', 'refresh')) OR "
            "(scope = 'sid' AND session_id_hash IS NOT NULL "
            "AND token_jti_hash IS NULL AND token_type IS NULL)",
            name="ck_token_revocations_scope_shape",
        ),
        CheckConstraint(
            "token_jti_hash IS NULL OR length(token_jti_hash) = 64",
            name="ck_token_revocations_jti_hash_length",
        ),
        CheckConstraint(
            "session_id_hash IS NULL OR length(session_id_hash) = 64",
            name="ck_token_revocations_sid_hash_length",
        ),
        # Composite index: "give me revocations for user X since T".
        # Cheaper than the per-column indexes for the common cold-start
        # replay pattern.
        Index(
            "ix_token_revocations_user_id_revoked_at",
            "user_id",
            "revoked_at",
        ),
        Index(
            "ix_token_revocations_jti_scope",
            "scope",
            "token_jti_hash",
        ),
        Index(
            "ix_token_revocations_sid_scope",
            "scope",
            "session_id_hash",
        ),
    )
