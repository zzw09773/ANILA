# -*- coding: utf-8 -*-
"""Durable Gate 5 session-event ledger.

The live :mod:`app.services.proxy.stream_bridge` validator deliberately keeps
no process state.  These two rows are the CSP-owned durable state behind its
``SessionEventStore`` contract:

* ``SessionEventRun`` is one locked cursor/terminal row for a trusted
  ``run_id`` and records the complete binding that was admitted for that run.
* ``SessionEvent`` is append-only canonical ``StepEvent`` JSON with indexed
  binding fields, a server cursor and an idempotency digest.

Identity fields are strings because the Gate 4 wire contract is intentionally
opaque (and legacy callers may use non-numeric IDs).  The adapter validates
the positive/numeric authority bindings before a formal dispatch reaches this
table; this model must not silently coerce or substitute them.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from app.database import Base

JSONValue = JSON().with_variant(JSONB, "postgresql")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SessionEventRun(Base):
    """One authoritative cursor and terminal latch for a trusted run."""

    __tablename__ = "session_event_runs"

    run_id = Column(String(255), primary_key=True)
    task_id = Column(String(255), nullable=False, index=True)
    trace_id = Column(String(255), nullable=False, index=True)
    agent_id = Column(String(255), nullable=False, index=True)
    session_id = Column(String(255), nullable=False, index=True)
    next_cursor = Column(Integer, nullable=False, default=0, server_default="0")
    last_source_sequence = Column(Integer, nullable=True)
    terminal_event_id = Column(String(255), nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    events = relationship(
        "SessionEvent",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="SessionEvent.cursor",
    )


class SessionEvent(Base):
    """One immutable canonical event in a trusted run."""

    __tablename__ = "session_events"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "event_id",
            name="uq_session_events_run_event_id",
        ),
        UniqueConstraint(
            "run_id",
            "cursor",
            name="uq_session_events_run_cursor",
        ),
        Index(
            "ix_session_events_binding_cursor",
            "task_id",
            "trace_id",
            "agent_id",
            "session_id",
            "run_id",
            "cursor",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(
        String(255),
        ForeignKey("session_event_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    task_id = Column(String(255), nullable=False, index=True)
    trace_id = Column(String(255), nullable=False, index=True)
    agent_id = Column(String(255), nullable=False, index=True)
    session_id = Column(String(255), nullable=False, index=True)
    event_id = Column(String(255), nullable=False)
    # ``cursor`` is CSP-owned; the value supplied by the Agent is retained in
    # the JSON only after the adapter replaces it with this integer cursor.
    cursor = Column(Integer, nullable=False)
    source_sequence = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False)
    is_terminal = Column(Boolean, nullable=False, default=False, server_default="false")
    payload_sha256 = Column(String(64), nullable=False)
    payload = Column(JSONValue, nullable=False)
    created_at = Column(DateTime, nullable=False, default=_utcnow)

    run = relationship("SessionEventRun", back_populates="events")


__all__ = ["SessionEvent", "SessionEventRun"]
