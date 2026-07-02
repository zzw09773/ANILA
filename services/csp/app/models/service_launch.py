# -*- coding: utf-8 -*-
"""ServiceLaunch + ServiceAuditCallback — the launch-gateway audit trail.

doc 07 §5/§6/§10. A ``service_launches`` row is created every time the Launch
Gateway mints a launch token; it records who launched what, with which task /
trace / classification, and the token's TTL window. ``consumed_at`` exists for
future server-side one-time semantics — in this slice services verify the
token locally via ``/.well-known/jwks.json`` + ``exp`` (doc §6 / §15.5), so the
gateway does not gate on it.

``service_audit_callbacks`` is append-only: registered services POST audit
events back to CSP (``POST /api/services/{service_id}/audit-callbacks``) with a
Service Client Token (doc 03 naming). Both FKs use ``ON DELETE SET NULL`` so
the audit trail survives service deletion (doc §14 preserve-history blocker).
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.types import JSON

from app.database import Base

_JSON_OBJ = JSON().with_variant(JSONB(), "postgresql")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ServiceLaunch(Base):
    __tablename__ = "service_launches"

    launch_id = Column(String(64), primary_key=True)
    service_id = Column(
        Integer,
        ForeignKey("registered_services.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    task_id = Column(
        Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    trace_id = Column(String(64), nullable=True, index=True)
    source_snapshot_id = Column(Integer, nullable=True)
    classification_level = Column(
        String(20), nullable=False, server_default="無機密"
    )
    mode = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False, server_default="issued")
    issued_at = Column(DateTime, nullable=False, default=_utcnow)
    expires_at = Column(DateTime, nullable=False)
    consumed_at = Column(DateTime, nullable=True)

    service = relationship("RegisteredService", foreign_keys=[service_id])


class ServiceAuditCallback(Base):
    __tablename__ = "service_audit_callbacks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    service_id = Column(
        Integer,
        ForeignKey("registered_services.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    launch_id = Column(
        String(64),
        ForeignKey("service_launches.launch_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    event_type = Column(String(100), nullable=False)
    payload = Column(_JSON_OBJ, nullable=True)
    classification_level = Column(String(20), nullable=True)
    integration_key_id = Column(
        Integer,
        ForeignKey("service_clients.id", ondelete="SET NULL"),
        nullable=True,
    )
    received_at = Column(DateTime, nullable=False, default=_utcnow)
