"""Alert delivery. Mail settings live in the console, not in the environment.

Detection calls :func:`notify_alert_opened` on each new open and when
severity rises. The notifier sends at most one message per fingerprint
while that severity stays the same, until the alert is resolved. A
delivery failure never breaks detection.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AlertNotification:
    """What would go on the wire. No raw endpoint addresses — callers must
    already have scrubbed ``message`` the same way health alerts do."""

    fingerprint: str
    category: str
    severity: str
    title: str
    message: str
    source_type: str | None = None
    source_id: str | None = None


class AlertNotifier(Protocol):
    def send(self, notification: AlertNotification) -> None:
        """Deliver one alert notification. Must not raise into detectors."""


class SmtpAlertNotifier:
    """Reads console SMTP settings and sends one mail per open fingerprint."""

    def send(self, notification: AlertNotification) -> None:
        from app.database import SessionLocal
        from app.services.alert_mail import deliver_open_alert

        db = SessionLocal()
        try:
            deliver_open_alert(db, notification)
            db.commit()
        except Exception:
            db.rollback()
            logger.exception(
                "alert mail notifier failed fingerprint=%s",
                notification.fingerprint,
            )
        finally:
            db.close()


_notifier: AlertNotifier = SmtpAlertNotifier()


def get_notifier() -> AlertNotifier:
    return _notifier


def set_notifier(notifier: AlertNotifier) -> AlertNotifier:
    """Test swap. Returns the previous notifier."""
    global _notifier
    previous = _notifier
    _notifier = notifier
    return previous


def notify_alert_opened(
    *,
    fingerprint: str,
    category: str,
    severity: str,
    title: str,
    message: str,
    source_type: str | None = None,
    source_id: str | int | None = None,
) -> None:
    """Invoke the configured notifier. Never raises into callers."""
    try:
        get_notifier().send(
            AlertNotification(
                fingerprint=fingerprint,
                category=category,
                severity=severity,
                title=title,
                message=message,
                source_type=source_type,
                source_id=str(source_id) if source_id is not None else None,
            )
        )
    except Exception:
        logger.exception(
            "alert notifier failed fingerprint=%s", fingerprint
        )
