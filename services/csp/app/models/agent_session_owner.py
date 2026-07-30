"""Per-session caller binding for CSP agent resume (P2.4 / H3)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from app.database import Base


class AgentSessionOwner(Base):
    """Maps an agent runtime ``session_id`` to the CSP user who owns it.

    Populated when ``/v1/chat/completions`` dispatches to an agent with
    ``anila_session_id``; consulted by
    ``POST /v1/agents/{name}/sessions/{id}/answer``.
    """

    __tablename__ = "agent_session_owners"

    session_id = Column(String(128), primary_key=True)
    owner_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
