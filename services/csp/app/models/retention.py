"""Durable singleton leases for classified-data retention workers."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RetentionReaperLease(Base):
    __tablename__ = "retention_reaper_leases"

    lease_name = Column(String(64), primary_key=True)
    lease_token = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
