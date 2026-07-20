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
from anila_contracts import Classification as ClassificationLevel

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
    classification_level: ClassificationLevel | str,
    integration_key_id: int | None,
) -> ServiceAuditCallback:
    """Append one callback with ``max(payload floor, launch level)``.

    A caller may raise the floor but can never lower the immutable launch
    context. A supplied launch id must exist and belong to the same service;
    malformed persisted launch classification fails closed.
    """
    if isinstance(classification_level, ClassificationLevel):
        payload_floor = classification_level
    elif isinstance(classification_level, str):
        payload_floor = ClassificationLevel.from_storage(classification_level)
    else:
        raise ValueError("audit callback classification_level must be explicit")

    levels = [payload_floor]
    if launch_id is not None:
        launch = (
            db.query(ServiceLaunch)
            .filter(ServiceLaunch.launch_id == launch_id)
            .first()
        )
        if launch is None:
            raise ValueError("audit callback launch_id does not exist")
        if launch.service_id != service_id:
            raise ValueError("audit callback launch_id belongs to another service")
        raw_launch_level = launch.classification_level
        if not isinstance(raw_launch_level, str):
            raise ValueError("service launch classification_level is invalid")
        levels.append(ClassificationLevel.from_storage(raw_launch_level))

    effective_level = ClassificationLevel.max_of(levels).to_storage()
    stored_payload = dict(payload or {})
    stored_payload["classification_level"] = effective_level
    row = ServiceAuditCallback(
        service_id=service_id,
        launch_id=launch_id,
        event_type=event_type,
        payload=stored_payload,
        classification_level=effective_level,
        integration_key_id=integration_key_id,
    )
    db.add(row)
    db.flush()
    return row
