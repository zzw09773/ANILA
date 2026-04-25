from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Boolean, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from app.database import Base


class PlatformLink(Base):
    __tablename__ = "platform_links"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    url = Column(String(500), nullable=False)
    icon = Column(String(50), nullable=True)
    description = Column(String(255), nullable=True)
    sort_order = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    # JSONB array of role names. [] = everyone (default); ['admin'] = admin
    # only; ['admin','developer'] = either role passes the gate. Required-role
    # check is the cheap first filter before the per-user/per-department
    # ServiceAccessGrant lookup.
    required_roles = Column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
