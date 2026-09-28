"""治理中心的警報寄信。密碼用既有的憑證加密，不進環境變數。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AlertMailSettings(Base):
    __tablename__ = "alert_mail_settings"

    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, nullable=False, default=False)
    smtp_host = Column(String(253), nullable=False, default="")
    smtp_port = Column(Integer, nullable=False, default=587)
    # none | starttls | ssl
    security = Column(String(20), nullable=False, default="starttls")
    username = Column(String(320), nullable=False, default="")
    # enc::v1::。NULL 表示沒有密碼。
    password_envelope = Column(Text, nullable=True)
    from_address = Column(String(320), nullable=False, default="")
    recipients = Column(Text, nullable=False, default="")
    last_error = Column(Text, nullable=True)
    last_error_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )


class AlertMailDelivery(Base):
    """同一段未解決期間的寄信紀錄。

    成功時 ``sent_at`` 有值，解決前不再寄。失敗時 ``sent_at`` 為空，
    背景迴圈依 ``next_retry_at`` 再試，三次之後停，直到解決後重開。
    """

    __tablename__ = "alert_mail_deliveries"

    fingerprint = Column(String(200), primary_key=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    next_retry_at = Column(DateTime(timezone=True), nullable=True)
    category = Column(String(50), nullable=True)
    severity = Column(String(20), nullable=True)
    title = Column(String(200), nullable=True)
    message = Column(Text, nullable=True)
    source_type = Column(String(50), nullable=True)
    source_id = Column(String(100), nullable=True)
