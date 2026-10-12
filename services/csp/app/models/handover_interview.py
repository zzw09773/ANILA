"""同仁離開專案前的交接訪談。

這不是對話轉交。一列是一位使用者自己的訪談：固定題目的回答、
是否已確認，以及確認之後才寫入的交接清單。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base

JSONValue = JSON().with_variant(JSONB, "postgresql")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class HandoverInterview(Base):
    __tablename__ = "handover_interviews"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_label = Column(String(80), nullable=False, default="")
    # answering：還在答。confirmed：本人已確認，清單仍是空的。
    # ready：確認之後才寫入清單。
    status = Column(String(20), nullable=False)
    answers = Column(JSONValue, nullable=False, default=dict)
    checklist = Column(JSONValue, nullable=True)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )
