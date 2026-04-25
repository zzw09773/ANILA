"""TTL-bounded pgvector credential issued to a developer for a single agent.

Each row corresponds to a Postgres role created out-of-band (CSP backend will
``CREATE ROLE`` + ``ALTER ROLE ... SET anila.agent_id`` so RLS policies on
``document_chunks`` auto-scope to the agent the developer is working on).
The TTL (default 30 days) keeps long-lived credentials from drifting into
"shared password" territory — the cron auto-revoker will set
``revoked_at = now()`` for any row past ``expires_at``, and a separate cron
fires the 7-day-before-expiry reminder using ``reminder_sent_at`` to ensure
each credential is reminded exactly once.

See ``migrations/versions/0012_add_service_access_control.py`` for the schema
and ``docs/multi-service-integration-plan.md`` §10.2 for the full lifecycle
(issue → reminder → auto-revoke → DROP ROLE).
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.database import Base


class DevDbCredential(Base):
    __tablename__ = "dev_db_credentials"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    agent_id = Column(
        Integer, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    # Globally unique because Postgres role names live in a flat namespace.
    pg_role_name = Column(String(100), nullable=False, unique=True)
    issued_at = Column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    # Stamped the moment the 7-day-before-expiry reminder is dispatched, so a
    # cron rerun doesn't double-send.
    reminder_sent_at = Column(DateTime, nullable=True)

    user = relationship("User", foreign_keys=[user_id])
    agent = relationship("Agent", foreign_keys=[agent_id])

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None and self.expires_at > datetime.now(timezone.utc)
