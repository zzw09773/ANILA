"""Daily purge of ``action_function_runs`` rows older than 360 days.

Spec §7.6 mandates 360-day retention for run audit (events_json +
request_payload_json). The high-level ``audit_logs`` entries written by
``audit_service.log_audit_event`` keep their own (longer) retention as
governed by CSP policy — this purge only touches the raw run audit
rows.

Deployment note: this function is meant to be invoked by a daily
scheduler. v1 ops can wire it via:

  * an arq cron (CSP already has ``arq`` in ``requirements.txt``), or
  * a docker-compose ``scheduler`` sidecar that runs ``python -c "..."``
    once a day, or
  * a host-level cron tab.

Whichever path is chosen, this module stays a pure DB function — no
scheduler library imports here so unit tests stay simple.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import ActionFunctionRun


DEFAULT_RETENTION_DAYS = 360


def purge_expired_runs(
    db: Session, retention_days: int = DEFAULT_RETENTION_DAYS
) -> int:
    """Delete runs older than ``retention_days``. Returns count deleted.

    The DELETE is bulk (``synchronize_session=False``) — we don't need
    SQLAlchemy to invalidate identity-map entries since the caller is
    typically a one-shot background job, not a long-lived session.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    deleted = (
        db.query(ActionFunctionRun)
        .filter(ActionFunctionRun.started_at < cutoff)
        .delete(synchronize_session=False)
    )
    db.commit()
    return int(deleted or 0)
