"""Alert delivery interface — SMTP left deliberately unwired (P3.2 / OWNER Q3).

Detection and the ``alerts`` ledger are real; sending waits on an SMTP relay
from another unit. Until then every open/reopen still calls this interface so
the missing wire is **visible in the console**, not silent.

When SMTP arrives: set ``ANILA_ALERT_SMTP_*`` (see ``app.config.Settings``)
and swap in a real notifier — no detector rewrite. Prefer a **group mailbox**
for ``ANILA_ALERT_SMTP_TO`` (same reason as PLAN 5.4 support address).
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
        """Deliver (or visibly refuse to deliver) one alert notification."""


class UnwiredSmtpNotifier:
    """Default notifier: SMTP not configured. Logs at WARNING so operators
    see the gap without mistaking it for a quiet healthy system."""

    def send(self, notification: AlertNotification) -> None:
        logger.warning(
            "ALERT_SMTP_UNWIRED fingerprint=%s severity=%s category=%s "
            "title=%s message=%s source=%s/%s "
            "(set ANILA_ALERT_SMTP_* + group mailbox TO when relay exists)",
            notification.fingerprint,
            notification.severity,
            notification.category,
            notification.title,
            notification.message,
            notification.source_type or "-",
            notification.source_id or "-",
        )


_notifier: AlertNotifier = UnwiredSmtpNotifier()


def get_notifier() -> AlertNotifier:
    return _notifier


def set_notifier(notifier: AlertNotifier) -> AlertNotifier:
    """Test / future SMTP swap. Returns the previous notifier."""
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
    except Exception:  # pragma: no cover - notifier must not break detect path
        logger.exception(
            "alert notifier failed fingerprint=%s", fingerprint
        )
