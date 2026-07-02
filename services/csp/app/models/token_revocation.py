"""Durable log of JWT token-revocation events.

Each row records one bump of ``users.token_version`` — whether it
came from logout, password change, or admin-initiated revoke. The
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

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Index, Integer

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
    revoked_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    __table_args__ = (
        # Composite index: "give me revocations for user X since T".
        # Cheaper than the per-column indexes for the common cold-start
        # replay pattern.
        Index(
            "ix_token_revocations_user_id_revoked_at",
            "user_id",
            "revoked_at",
        ),
    )
