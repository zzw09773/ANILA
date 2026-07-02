"""TrustedHost ORM — admin-managed SSRF guard allow-list.

Backed by migration 0034. Each row is a hostname that the SSRF guard
short-circuits past its host validation (deny lists, single-label,
internal-zone suffix). Scheme + structurally-unsafe IP checks still
apply.

Lookup is by ``host`` (case-insensitive, normalised lowercase at the
service layer). Reads go through ``trusted_host_service`` which keeps
an in-memory cache; the SSRF guard (anila-core ``url_guard``) gets
its data via the cached provider hook, not by querying this table
directly.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.database import Base


class TrustedHost(Base):
    __tablename__ = "trusted_hosts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    host = Column(String(255), nullable=False, unique=True, index=True)
    note = Column(Text, nullable=True)

    created_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    created_by = relationship("User", foreign_keys=[created_by_user_id])
