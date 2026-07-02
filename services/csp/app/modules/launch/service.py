# -*- coding: utf-8 -*-
"""Launch Gateway primitives — service_launches rows, launch URLs, audit rows.

Pure launch mechanics with NO policy/task/api coupling (module-boundary
contract: ``app.modules.launch`` must not import ``app.modules.policy`` /
``app.modules.tasks`` / ``app.api``). The API layer (``app/api/services.py``)
orchestrates access control + PolicyDecision + audit around these primitives.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse, urlunparse
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.service_launch import ServiceAuditCallback, ServiceLaunch

from .token import LAUNCH_TOKEN_TTL_MINUTES


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_launch_id() -> str:
    return f"launch_{uuid4().hex}"


def new_trace_id() -> str:
    return f"trace_{uuid4().hex}"


def create_service_launch(
    db: Session,
    *,
    launch_id: str,
    service_id: int,
    user_id: int | None,
    task_id: int | None,
    trace_id: str | None,
    classification_level: str,
    source_snapshot_id: int | None,
    mode: str,
    ttl_minutes: int = LAUNCH_TOKEN_TTL_MINUTES,
) -> ServiceLaunch:
    """Append a ``service_launches`` row (status=issued) and return it.

    ``issued_at`` / ``expires_at`` define the token TTL window the row is the
    server-side record of. Commit is the caller's (the API records a
    PolicyDecision + audit in the same flow).
    """
    issued_at = _utcnow()
    expires_at = issued_at + timedelta(minutes=ttl_minutes)
    row = ServiceLaunch(
        launch_id=launch_id,
        service_id=service_id,
        user_id=user_id,
        task_id=task_id,
        trace_id=trace_id,
        classification_level=classification_level,
        source_snapshot_id=source_snapshot_id,
        mode=mode,
        status="issued",
        issued_at=issued_at,
        expires_at=expires_at,
    )
    db.add(row)
    db.flush()
    return row


def build_launch_url(entry_url: str, token: str) -> str:
    """Append the launch token to the service entry URL as a query param.

    doc §5 returns "iframe URL + launch token"; we deliver the token via the
    ``launch_token`` query parameter (preserving any existing query string) so
    both iframe and new_tab modes carry it. The token is also returned
    standalone in the launch response for shells that prefer fragment delivery.
    """
    parts = urlparse(entry_url)
    existing = parts.query
    extra = urlencode({"launch_token": token})
    query = f"{existing}&{extra}" if existing else extra
    return urlunparse(parts._replace(query=query))


def record_service_audit_callback(
    db: Session,
    *,
    service_id: int | None,
    launch_id: str | None,
    event_type: str,
    payload: dict | None,
    classification_level: str | None,
    integration_key_id: int | None,
) -> ServiceAuditCallback:
    """Append one ``service_audit_callbacks`` row (append-only, no update)."""
    row = ServiceAuditCallback(
        service_id=service_id,
        launch_id=launch_id,
        event_type=event_type,
        payload=payload,
        classification_level=classification_level,
        integration_key_id=integration_key_id,
    )
    db.add(row)
    db.flush()
    return row
