"""平台模型角色：一個角色對應至多一個模型。

既有的 ``is_router_primary``／``is_platform_embedding``／``is_slides_primary``
仍寫在 ``model_registry`` 上（讀寫點很多，搬欄位不會更簡單）。視覺、摘要、
知識庫對話、生圖放這張表。治理中心與執行期都走同一組角色名稱。
列不存在，或 ``model_id`` 為空，就是「尚未設定」。
"""
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.database import Base


class ModelRole(Base):
    __tablename__ = "model_roles"

    role = Column(String(64), primary_key=True)
    model_id = Column(
        Integer,
        ForeignKey("model_registry.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    model = relationship("ModelRegistry")
