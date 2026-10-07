"""治理中心的人資資料庫連線。一列就夠。

密碼用外部服務同一套 ``enc::ext1::``，不進 ``platform_settings``。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON

from app.database import Base

DEFAULT_ROOT_UNIT_NAME = "國家中山科學研究院"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _empty_titles() -> list[str]:
    return []


class HrOracleSettings(Base):
    __tablename__ = "hr_oracle_settings"

    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, nullable=False, default=False, server_default="false")
    host = Column(String(255), nullable=False, default="", server_default="")
    port = Column(Integer, nullable=False, default=1521, server_default="1521")
    service_name = Column(String(128), nullable=False, default="", server_default="")
    db_user = Column(String(128), nullable=False, default="", server_default="")
    password_envelope = Column(Text, nullable=True)
    table_name = Column(String(256), nullable=False, default="", server_default="")
    # 打開時，人資有職稱就授與。關掉時，這個人下次登入收回 source 為 hr 的列。
    auto_unit_admin = Column(Boolean, nullable=False, default=True, server_default="true")
    auto_declass = Column(Boolean, nullable=False, default=True, server_default="true")
    # 留空表示任何職稱都算。有填才只限完全相符的職稱。
    unit_admin_titles = Column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
        default=_empty_titles,
    )
    declass_titles = Column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
        default=_empty_titles,
    )
    # 一級單位都掛在這個最上層單位之下。沒有根時用這個名稱建立。
    root_unit_name = Column(
        String(100),
        nullable=False,
        default=DEFAULT_ROOT_UNIT_NAME,
        server_default=DEFAULT_ROOT_UNIT_NAME,
    )
    health_status = Column(String(20), nullable=False, default="unknown", server_default="unknown")
    health_checked_at = Column(DateTime(timezone=True), nullable=True)
    health_detail = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)
    updated_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
