"""每位管理員自己的使用者回饋已讀水位。"""
from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Integer

from app.database import Base


class FeedbackReadState(Base):
    __tablename__ = "feedback_read_states"

    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    read_at = Column(DateTime(timezone=True), nullable=False)
