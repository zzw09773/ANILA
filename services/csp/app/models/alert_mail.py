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
    """同一段未解決期間，一個指紋只寄一封。解決時刪掉，重開才再寄。"""

    __tablename__ = "alert_mail_deliveries"

    fingerprint = Column(String(200), primary_key=True)
    sent_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
