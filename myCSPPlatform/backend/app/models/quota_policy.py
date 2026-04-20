from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, Boolean
from app.database import Base


class QuotaPolicy(Base):
    """Configurable quota + rate-limit policy assignable to users or API keys.

    Limits (all nullable = unlimited):
      token_limit_per_day      – max total tokens in a rolling 24 h window
      token_limit_per_month    – max total tokens in a calendar month
      request_limit_per_minute – max proxy requests per minute (in-memory window)
      request_limit_per_hour   – max proxy requests per hour (in-memory window)
    """

    __tablename__ = "quota_policies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False, unique=True, index=True)
    description = Column(String(500), nullable=True)

    token_limit_per_day = Column(Integer, nullable=True)
    token_limit_per_month = Column(Integer, nullable=True)
    request_limit_per_minute = Column(Integer, nullable=True)
    request_limit_per_hour = Column(Integer, nullable=True)

    is_default = Column(Boolean, nullable=False, default=False, server_default="false")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
