"""用量額度與被擋下的紀錄。

上限留空是無限，也是預設。金額上限的 ``limit_value`` 是 micros；
token 上限是顆數。同一對象、期間、計量只留一列。三種對象各一條
部分唯一索引，NULL 不會讓不同對象撞在一起。
"""

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UsageQuota(Base):
    __tablename__ = "usage_quotas"
    __table_args__ = (
        Index("ix_usage_quotas_user", "user_id"),
        Index("ix_usage_quotas_department", "department_id"),
        Index("ix_usage_quotas_api_key", "api_key_id"),
        Index(
            "uq_usage_quotas_user_period_metric",
            "user_id",
            "period",
            "metric",
            unique=True,
            sqlite_where=text("scope_type = 'user'"),
            postgresql_where=text("scope_type = 'user'"),
        ),
        Index(
            "uq_usage_quotas_unit_period_metric",
            "department_id",
            "period",
            "metric",
            unique=True,
            sqlite_where=text("scope_type = 'unit'"),
            postgresql_where=text("scope_type = 'unit'"),
        ),
        Index(
            "uq_usage_quotas_api_key_period_metric",
            "api_key_id",
            "period",
            "metric",
            unique=True,
            sqlite_where=text("scope_type = 'api_key'"),
            postgresql_where=text("scope_type = 'api_key'"),
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    scope_type = Column(String(16), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    department_id = Column(
        Integer, ForeignKey("departments.id", ondelete="CASCADE"), nullable=True
    )
    api_key_id = Column(
        Integer, ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=True
    )
    period = Column(String(16), nullable=False)
    metric = Column(String(16), nullable=False)
    limit_value = Column(BigInteger, nullable=True)
    warn_percent = Column(Integer, nullable=False, default=80, server_default="80")
    on_limit = Column(String(16), nullable=False, default="warn", server_default="warn")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    created_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class QuotaEvent(Base):
    """被擋下的呼叫。用量頁用來顯示是誰、哪一條額度。"""

    __tablename__ = "quota_events"
    __table_args__ = (
        Index("ix_quota_events_occurred", "occurred_at"),
        Index("ix_quota_events_user", "user_id"),
        Index("ix_quota_events_department", "department_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    quota_id = Column(
        Integer, ForeignKey("usage_quotas.id", ondelete="SET NULL"), nullable=True
    )
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    api_key_id = Column(
        Integer, ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True
    )
    department_id = Column(
        Integer, ForeignKey("departments.id", ondelete="SET NULL"), nullable=True
    )
    action = Column(String(16), nullable=False, default="block")
    message = Column(Text, nullable=False)
    occurred_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
