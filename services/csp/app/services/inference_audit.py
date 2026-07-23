"""End-user inference audit writes (fail-open unless ANILA_AUDIT_STRICT=1).

Timing contract (encoded once here — entrances must not re-branch on the flag):

* ``record_at_acceptance`` — durable write *before* inference side effects when
  ``ANILA_AUDIT_STRICT=1`` or the call is a stream (``stream=True``). Row is
  ``status=success`` with ``metadata.phase=acceptance``. For chat/agent this
  must precede server-side retrieval and memory embedding, not only upstream.
* ``record_at_outcome`` — write at the outcome point (success / denied / error)
  for non-strict non-stream paths. When acceptance already recorded, this is a
  no-op so exactly-one-row holds (outcome fidelity is the documented tradeoff
  under strict write-ahead).

Denied paths that reject *before* the acceptance call site still use
``record_at_outcome`` / ``record_inference_audit`` with ``acceptance_recorded=False``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.models.user import User
from app.services.audit_service import log_audit_event
from app.services.client_ip import resolve_client_ip

logger = logging.getLogger(__name__)

INFERENCE_ACTIONS = frozenset(
    {
        "inference.chat",
        "inference.rag_query",
        "inference.studio",
        "inference.agent",
        "inference.image",
    }
)


def short_audit_reason(code: str) -> str:
    """Normalize a short metadata.reason code (no stack traces / newlines)."""
    return " ".join(str(code).split())[:80]


def should_record_end_user_inference(
    request: Request | None,
    *,
    internal_router: bool = False,
) -> bool:
    """True only for direct end-user identity (JWT / sk-), not service hops."""
    if internal_router:
        return False
    state = getattr(request, "state", None) if request is not None else None
    if state is not None and getattr(state, "suppress_inference_audit", False):
        return False
    if state is not None and getattr(state, "csp_caller", None) is not None:
        return False
    return True


def record_inference_audit(
    db: Session,
    *,
    request: Request | None,
    actor: User | None,
    action: str,
    resource_id: str | int | None,
    detail: str | None,
    status: str = "success",
    metadata: dict[str, Any] | None = None,
    commit: bool = True,
    internal_router: bool = False,
    force: bool = False,
) -> None:
    """Write one inference audit row.

    Fail-open by default (log + continue). When ``ANILA_AUDIT_STRICT=1``,
    raise HTTP 503 so the request fails closed.
    """
    if action not in INFERENCE_ACTIONS:
        raise ValueError(f"unsupported inference audit action: {action}")
    if not force and not should_record_end_user_inference(
        request, internal_router=internal_router
    ):
        return
    if actor is None:
        return

    try:
        event = log_audit_event(
            db,
            action=action,
            resource_type="inference",
            actor=actor,
            resource_id=resource_id,
            status=status,
            detail=detail,
            ip_address=resolve_client_ip(request),
            metadata=metadata,
            commit=commit,
        )
        if event is None and settings.ANILA_AUDIT_STRICT:
            raise HTTPException(
                status_code=503,
                detail="審計紀錄寫入失敗，請求已拒絕",
            )
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "inference audit write failed: action=%s actor=%s",
            action,
            actor.username if actor else None,
        )
        if settings.ANILA_AUDIT_STRICT:
            raise HTTPException(
                status_code=503,
                detail="審計紀錄寫入失敗，請求已拒絕",
            ) from None


def record_at_acceptance(
    db: Session,
    *,
    request: Request | None,
    actor: User | None,
    action: str,
    resource_id: str | int | None,
    detail: str | None,
    metadata: dict[str, Any] | None = None,
    commit: bool = True,
    internal_router: bool = False,
    force: bool = False,
    stream: bool = False,
) -> bool:
    """Write acceptance-phase success before side effects when required.

    Writes when ``ANILA_AUDIT_STRICT`` or ``stream`` is true. The row uses
    ``status='success'`` and ``metadata.phase='acceptance'`` (committed when
    ``commit=True``, before SSE / upstream begins).

    Returns True iff a row was written — callers must then skip
    ``record_at_outcome`` so exactly-one-row holds.
    """
    if not (settings.ANILA_AUDIT_STRICT or stream):
        return False
    meta = dict(metadata or {})
    meta["phase"] = "acceptance"
    record_inference_audit(
        db,
        request=request,
        actor=actor,
        action=action,
        resource_id=resource_id,
        detail=detail,
        status="success",
        metadata=meta,
        commit=commit,
        internal_router=internal_router,
        force=force,
    )
    return True


def record_at_outcome(
    db: Session,
    *,
    request: Request | None,
    actor: User | None,
    action: str,
    resource_id: str | int | None,
    detail: str | None,
    status: str,
    metadata: dict[str, Any] | None = None,
    commit: bool = True,
    internal_router: bool = False,
    force: bool = False,
    acceptance_recorded: bool = False,
) -> None:
    """Write an outcome row unless acceptance already recorded.

    When ``acceptance_recorded`` is True (strict write-ahead or stream
    acceptance), this is a no-op — including post-upstream error/denied —
    preserving exactly-one-row. Denied paths that reject before the
    acceptance call site pass ``acceptance_recorded=False``.
    """
    if acceptance_recorded:
        return
    record_inference_audit(
        db,
        request=request,
        actor=actor,
        action=action,
        resource_id=resource_id,
        detail=detail,
        status=status,
        metadata=metadata,
        commit=commit,
        internal_router=internal_router,
        force=force,
    )
