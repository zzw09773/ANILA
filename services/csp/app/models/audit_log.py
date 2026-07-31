from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, Text
from app.database import Base


class AuditLog(Base):
    """稽核事件;append-only(P2.7,migration ``r1_0027``)。

    ``actor_user_id`` **刻意沒有 FK**。原本它是 ``users.id`` 的外鍵,於是
    硬刪一個帳號就必須先把稽核列的 actor 清成 NULL(``app/api/users.py``)
    —— 也就是「刪掉自己的帳號就能洗掉自己在稽核帳上的數字身分」,正是威脅
    模型裡那個人最想要的功能。拿掉 FK 之後,欄位與值原地保留,稽核歸屬不再
    因為帳號被刪而消失;users 表那邊也不再需要對稽核表發 UPDATE
    (append-only 觸發器會擋)。
    """

    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    actor_user_id = Column(Integer, nullable=True, index=True)
    actor_username = Column(String(100), nullable=True, index=True)
    action = Column(String(100), nullable=False, index=True)
    resource_type = Column(String(50), nullable=False, index=True)
    resource_id = Column(String(100), nullable=True)
    status = Column(String(20), nullable=False, default="success", index=True)
    detail = Column(Text, nullable=True)
    ip_address = Column(String(64), nullable=True)
    metadata_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
