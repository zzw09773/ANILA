"""警報寄信：設定存在資料庫，密碼用既有的憑證加密。

寄失敗只記日誌與畫面上的錯誤，不往偵測器丟例外。
主機檢查走跟其他出向位址同一套 ``validate_outbound_url``。
"""
from __future__ import annotations

import base64
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from anila_core.security.credential_crypto import decrypt_credential, encrypt_credential
from anila_core.security.url_guard import UnsafeEndpointError, validate_outbound_url

from app.models.alert import Alert
from app.models.alert_mail import AlertMailDelivery, AlertMailSettings
from app.models.user import User
from app.services.audit_service import log_audit_event
from app.time_utils import as_utc

logger = logging.getLogger(__name__)

ENVELOPE_PREFIX = "enc::v1::"
_SECURITY = frozenset({"none", "starttls", "ssl"})
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SEVERITY_LABEL = {
    "critical": "嚴重",
    "high": "高",
    "medium": "中",
    "low": "低",
}
# 第一次失敗後 10 分鐘、第二次後再 20 分鐘，第三次停。三次橫跨約 30 分鐘。
MAIL_MAX_ATTEMPTS = 3
_RETRY_AFTER_SECONDS = (600, 1200)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AlertMailConfigError(ValueError):
    def __init__(self, message: str):
        super().__init__(message)
        self.public_message = message


def seal_password(plaintext: str) -> str:
    ciphertext, nonce, tag = encrypt_credential(plaintext)
    blob = base64.urlsafe_b64encode(nonce + tag + ciphertext).decode("ascii")
    return f"{ENVELOPE_PREFIX}{blob}"


def open_password(stored: str | None) -> str:
    if not stored:
        return ""
    if not stored.startswith(ENVELOPE_PREFIX):
        raise ValueError("警報寄信密碼密封損毀")
    raw = base64.urlsafe_b64decode(stored[len(ENVELOPE_PREFIX) :].encode("ascii"))
    if len(raw) < 12 + 16:
        raise ValueError("警報寄信密碼密封過短")
    nonce, tag, ciphertext = raw[:12], raw[12:28], raw[28:]
    return decrypt_credential(ciphertext, nonce, tag)


def validate_smtp_host(host: str) -> None:
    """同一套出向檢查：把主機當成 https 位址看 scheme 以外的規則。"""
    cleaned = (host or "").strip().lower().rstrip(".")
    if not cleaned or any(char in cleaned for char in " \t\r\n/@\\"):
        raise UnsafeEndpointError("SMTP 主機格式不正確", reason="no_hostname")
    if ":" in cleaned or cleaned.startswith("["):
        raise UnsafeEndpointError(
            "SMTP 主機不要包含連接埠",
            host=cleaned,
            reason="no_hostname",
        )
    validate_outbound_url(f"https://{cleaned}/")


def parse_recipients(raw: str | None) -> list[str]:
    parts = re.split(r"[,;\s]+", (raw or "").strip())
    return [part for part in parts if part]


def public_view(row: AlertMailSettings) -> dict:
    return {
        "enabled": bool(row.enabled),
        "smtp_host": row.smtp_host or "",
        "smtp_port": int(row.smtp_port or 587),
        "security": row.security or "starttls",
        "username": row.username or "",
        "has_password": bool(row.password_envelope),
        "from_address": row.from_address or "",
        "recipients": row.recipients or "",
        "last_error": row.last_error,
        "last_error_at": row.last_error_at,
    }


def ensure_settings(db: Session) -> AlertMailSettings:
    row = db.query(AlertMailSettings).filter(AlertMailSettings.id == 1).one_or_none()
    if row is None:
        row = AlertMailSettings(
            id=1,
            enabled=False,
            smtp_host="",
            smtp_port=587,
            security="starttls",
            username="",
            from_address="",
            recipients="",
            updated_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.flush()
    return row


def save_mail_settings(
    db: Session,
    *,
    enabled: bool,
    smtp_host: str,
    smtp_port: int,
    security: str,
    username: str,
    password: str | None,
    password_set: bool,
    clear_password: bool,
    from_address: str,
    recipients: str,
    actor: User | None,
) -> AlertMailSettings:
    row = ensure_settings(db)
    row.enabled = bool(enabled)
    row.smtp_host = (smtp_host or "").strip()
    row.smtp_port = int(smtp_port)
    row.security = (security or "").strip().lower()
    row.username = (username or "").strip()
    row.from_address = (from_address or "").strip()
    row.recipients = recipients or ""
    if password_set:
        row.password_envelope = seal_password(password or "")
    elif clear_password:
        row.password_envelope = None
    row.updated_at = datetime.now(timezone.utc)
    row.updated_by_user_id = actor.id if actor is not None else None
    _validate_row(row, for_send=False)
    log_audit_event(
        db,
        actor=actor,
        action="alert_mail_update",
        resource_type="alert_mail",
        resource_id=row.id,
        detail=(
            f"enabled={row.enabled} host={row.smtp_host} port={row.smtp_port} "
            f"security={row.security} from={row.from_address}"
        ),
    )
    db.flush()
    return row


def send_test_mail(db: Session) -> str | None:
    """成功回 None。SMTP 失敗回給畫面的錯誤文字，不丟例外。設定不完整則丟 AlertMailConfigError。"""
    row = ensure_settings(db)
    _validate_row(row, for_send=True)
    try:
        _transmit(
            row,
            subject="ANILA 警報寄信測試",
            body=(
                "這是一封測試信。\n"
                "ANILA 警報寄信可以連上這台郵件伺服器。\n"
                "若你收到這封信，請回到治理中心打開「啟用」。\n"
            ),
        )
    except Exception as exc:
        _remember_failure(row, exc)
        db.flush()
        return _public_error(row, exc)
    row.last_error = None
    row.last_error_at = None
    db.flush()
    return None


def deliver_open_alert(db: Session, notification) -> None:
    """新開的警報寄一封。同一指紋在解決前不立刻重寄。失敗不往外丟。"""
    try:
        row = ensure_settings(db)
        if not row.enabled:
            return
        existing = (
            db.query(AlertMailDelivery)
            .filter(AlertMailDelivery.fingerprint == notification.fingerprint)
            .one_or_none()
        )
        if existing is not None:
            return
        delivery = AlertMailDelivery(
            fingerprint=notification.fingerprint,
            sent_at=None,
            attempt_count=0,
            category=getattr(notification, "category", None),
            severity=getattr(notification, "severity", None),
            title=getattr(notification, "title", None),
            message=getattr(notification, "message", None),
            source_type=getattr(notification, "source_type", None),
            source_id=getattr(notification, "source_id", None),
        )
        db.add(delivery)
        db.flush()
        _attempt_send(db, row, delivery, notification)
    except Exception:
        logger.exception(
            "警報寄信失敗且無法寫回錯誤 fingerprint=%s",
            getattr(notification, "fingerprint", "-"),
        )


def retry_due_alert_mail(db: Session | None = None) -> None:
    """背景迴圈呼叫。到期的失敗信再試一次，成功或用盡次數就停。"""
    own = db is None
    if own:
        from app.database import SessionLocal

        db = SessionLocal()
    try:
        now = _now()
        settings = ensure_settings(db)
        if not settings.enabled:
            if own:
                db.commit()
            return
        pending = (
            db.query(AlertMailDelivery)
            .filter(AlertMailDelivery.sent_at.is_(None))
            .all()
        )
        from app.services.alert_notifier import AlertNotification

        for delivery in pending:
            due = as_utc(delivery.next_retry_at)
            if due is None or due > now:
                continue
            if int(delivery.attempt_count or 0) >= MAIL_MAX_ATTEMPTS:
                continue
            alert = (
                db.query(Alert)
                .filter(Alert.fingerprint == delivery.fingerprint)
                .one_or_none()
            )
            if alert is not None and alert.status == "resolved":
                continue
            _attempt_send(
                db,
                settings,
                delivery,
                AlertNotification(
                    fingerprint=delivery.fingerprint,
                    category=delivery.category or "",
                    severity=delivery.severity or "",
                    title=delivery.title or "",
                    message=delivery.message or "",
                    source_type=delivery.source_type,
                    source_id=delivery.source_id,
                ),
            )
        if own:
            db.commit()
    except Exception:
        if own:
            db.rollback()
        logger.exception("警報寄信重試這輪失敗")
    finally:
        if own:
            db.close()


def _attempt_send(db: Session, row: AlertMailSettings, delivery: AlertMailDelivery, notification) -> None:
    try:
        _validate_row(row, for_send=True)
        label = _SEVERITY_LABEL.get(notification.severity, notification.severity)
        _transmit(
            row,
            subject=f"ANILA 警報（{label}）：{notification.title}",
            body=(
                f"{notification.title}\n\n"
                f"{notification.message}\n\n"
                f"分類：{notification.category}\n"
                f"嚴重度：{label}\n"
                f"識別：{notification.fingerprint}\n"
            ),
        )
    except Exception as exc:
        _remember_failure(row, exc)
        delivery.attempt_count = int(delivery.attempt_count or 0) + 1
        if delivery.attempt_count >= MAIL_MAX_ATTEMPTS:
            delivery.next_retry_at = None
        else:
            wait = _RETRY_AFTER_SECONDS[delivery.attempt_count - 1]
            delivery.next_retry_at = _now() + timedelta(seconds=wait)
        logger.error(
            "警報寄信失敗 fingerprint=%s attempt=%s error=%s",
            delivery.fingerprint,
            delivery.attempt_count,
            _public_error(row, exc),
        )
        db.flush()
        return
    delivery.sent_at = _now()
    delivery.next_retry_at = None
    row.last_error = None
    row.last_error_at = None
    db.flush()


def _validate_row(row: AlertMailSettings, *, for_send: bool) -> None:
    if row.security not in _SECURITY:
        raise AlertMailConfigError("連線安全請選不加密、STARTTLS 或 SSL")
    try:
        port = int(row.smtp_port)
    except (TypeError, ValueError) as exc:
        raise AlertMailConfigError("連接埠必須是 1 到 65535 的數字") from exc
    if port < 1 or port > 65535:
        raise AlertMailConfigError("連接埠必須是 1 到 65535 的數字")
    host = (row.smtp_host or "").strip()
    if host:
        validate_smtp_host(host)
    if not for_send and not row.enabled:
        return
    if not host:
        raise AlertMailConfigError("請填 SMTP 主機")
    sender = (row.from_address or "").strip()
    if not _EMAIL.match(sender):
        raise AlertMailConfigError("寄件者要是一個信箱位址")
    recipients = parse_recipients(row.recipients)
    if not recipients:
        raise AlertMailConfigError("請填至少一個收件者，建議用群組信箱")
    if len(recipients) > 20:
        raise AlertMailConfigError("收件者最多 20 個")
    for address in recipients:
        if not _EMAIL.match(address):
            raise AlertMailConfigError(f"收件者格式不正確：{address}")
    if (row.username or "").strip() and not row.password_envelope:
        raise AlertMailConfigError("有帳號時要一併填密碼")


def _smtp_tls_context():
    """跟模型呼叫同一套信任庫：有內部 CA 檔就只用那一份。"""
    import os
    import ssl

    for key in ("SSL_CERT_FILE", "ANILA_MODEL_CA_FILE"):
        path = (os.environ.get(key) or "").strip()
        if path and os.path.isfile(path):
            return ssl.create_default_context(cafile=path)
    return ssl.create_default_context()


def _transmit(row: AlertMailSettings, *, subject: str, body: str) -> None:
    import smtplib
    from email.message import EmailMessage

    recipients = parse_recipients(row.recipients)
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = row.from_address.strip()
    message["To"] = ", ".join(recipients)
    message.set_content(body, charset="utf-8", cte="8bit")
    host = row.smtp_host.strip()
    port = int(row.smtp_port)
    tls = _smtp_tls_context() if row.security in ("ssl", "starttls") else None
    if row.security == "ssl":
        client = smtplib.SMTP_SSL(host, port, timeout=20, context=tls)
    else:
        client = smtplib.SMTP(host, port, timeout=20)
    try:
        client.ehlo()
        if row.security == "starttls":
            client.starttls(context=tls)
            client.ehlo()
        username = (row.username or "").strip()
        if username:
            client.login(username, open_password(row.password_envelope))
        client.sendmail(row.from_address.strip(), recipients, message.as_bytes())
    finally:
        try:
            client.quit()
        except Exception:
            try:
                client.close()
            except Exception:
                pass


def _remember_failure(row: AlertMailSettings, exc: BaseException) -> None:
    row.last_error = _public_error(row, exc)
    row.last_error_at = datetime.now(timezone.utc)


def _public_error(row: AlertMailSettings, exc: BaseException) -> str:
    if isinstance(exc, AlertMailConfigError):
        text = exc.public_message
    else:
        chunks: list[str] = []
        for arg in exc.args or (str(exc),):
            if isinstance(arg, bytes):
                chunks.append(arg.decode("utf-8", "replace"))
            else:
                chunks.append(str(arg))
        text = " ".join(chunks) or str(exc)
    return _redact(text, row)[:500]


def _redact(text: str, row: AlertMailSettings) -> str:
    secrets: list[str] = []
    username = (row.username or "").strip()
    password = open_password(row.password_envelope) if row.password_envelope else ""
    if username:
        secrets.append(username)
    if password:
        secrets.append(password)
    # 較長者先換，避免帳號是密碼的子字串時先被換掉、剩下的密碼片段漏出。
    redacted = text
    seen: set[str] = set()
    for secret in sorted(secrets, key=len, reverse=True):
        if not secret or secret in seen:
            continue
        seen.add(secret)
        redacted = redacted.replace(secret, "[已隱藏]")
    return redacted
