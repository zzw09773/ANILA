"""Durable, one-time state for smart-card login challenges.

The signed challenge JWT is still the client-facing carrier.  This table is
the shared consume-once authority used by every CSP worker, so replay safety
does not depend on process-local memory or worker affinity.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String

from app.database import Base


class CardLoginChallenge(Base):
    __tablename__ = "card_login_challenges"

    # Random JWT ID. It is not secret, but gives the database a compact,
    # indexed handle for an atomic consume operation.
    jti = Column(String(64), primary_key=True)
    # Store only a digest of the nonce; the plaintext already travels to the
    # browser and does not need another durable copy in the database.
    nonce_digest = Column(String(64), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
