from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, Index
from sqlalchemy.orm import relationship
from app.database import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_action_created_at", "action", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    actor_username = Column(String(100), nullable=True, index=True)
    action = Column(String(100), nullable=False, index=True)
    resource_type = Column(String(50), nullable=False, index=True)
    resource_id = Column(String(100), nullable=True)
    status = Column(String(20), nullable=False, default="success", index=True)
    detail = Column(Text, nullable=True)
    ip_address = Column(String(64), nullable=True)
    metadata_json = Column(Text, nullable=True)
    # timezone=True:W2-10 批次 1(migration r1_0040)。這個欄在乾淨 alembic 鏈上
    # 從 `0001` 起就是 timestamptz,錯的只有 ORM 宣告 —— 讀回來被降級成 naive,
    # 拿去跟 aware 比較就 TypeError(W1-4 那個 API key 500 的同一個病根)。
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )

    actor = relationship("User", lazy="joined")
