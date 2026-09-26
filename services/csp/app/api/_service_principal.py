"""Admit / reject service-token principals by kind (and optional client_type).

Platform s2s endpoints used to call ``verify_service_token`` and then act
without asking *what kind of principal* the token belonged to. An
agent-kind ``csk-`` therefore satisfied endpoints meant for router /
studio / asr-gateway. This helper makes the admitted kinds explicit.

``CallerIdentity.kind`` is ``"agent"`` | ``"service_client"``.
``service_clients.client_type`` is one of ``router`` | ``worker`` |
``studio`` | ``admin_tool`` (see ``app.api.service_clients._CLIENT_TYPES``).

Live fleet topology (measured against the running stack)
========================================================

Migration ``0027`` seeded the host ``CSP_SERVICE_TOKEN`` into
``service_clients`` as ``client_name='router-primary'``,
``client_type='router'``, ``is_legacy=TRUE``. ``verify_service_token``
matches ``service_clients`` *before* the env fallback, so in the live
stack the shared fleet secret resolves as an attributed
``service_client`` (not ``identity is None``). csp / router /
anila-studio / asr-gateway all present that same secret; three of them
are therefore indistinguishable from ``client_type='router'``.

具名已接受風險（OWNER 2026-09-19）
==================================

同一把 fleet secret、三服務（router / anila-studio / asr-gateway）
不可彼此辨識。kind gate 不隔離這三者；這不是漏修，是已接受的部署現況。
重寫部署腳本、改成每服務一把 token 時再拆。在那之前不要把
``client_type='router'`` 讀成「呼叫者就是 Router」。

``identity is None`` means the env-var fallback branch actually ran —
i.e. no active ``service_clients`` / ``agent_credentials`` row matched.
That path is effectively dead while the seeded ``router-primary`` row
stays active. Callers may still pass ``allow_legacy_env=True`` for
cutover / tests, but:

* This gate rejects per-agent ``csk-`` (``kind='agent'``). That is the
  real forward-looking buy.
* It does **not** isolate fleet-secret holders from each other — they
  all share the ``router`` attribution today.
* When ``allowed_client_types`` is set, ``identity is None`` is refused
  (fail-closed): an unattributed principal cannot satisfy a
  ``client_type`` restriction. Otherwise deactivating the owning
  ``service_clients`` row would silently widen admission via the env
  fallback.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Collection, Optional, Sequence

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.service_client import ServiceClient
from app.services.audit_service import log_audit_event

if TYPE_CHECKING:
    from app.services.agent_credential_service import CallerIdentity

logger = logging.getLogger(__name__)

# Audit actions for kind-gate observability (no token / no model URL).
AUDIT_KIND_DENIED = "service_principal_kind_denied"
AUDIT_LEGACY_ENV_ADMITTED = "service_principal_legacy_env_admitted"


def _as_str_set(values: Collection[str], *, param: str) -> frozenset[str]:
    """Normalize an allow-list; reject bare ``str`` (iterable of chars)."""
    if isinstance(values, str):
        raise TypeError(
            f"{param} must be a sequence/set of strings, not a bare str"
        )
    return frozenset(values)


def require_admitted_service_principal(
    identity: Optional["CallerIdentity"],
    *,
    db: Session,
    allowed_kinds: Collection[str] = ("service_client",),
    allowed_client_types: Sequence[str] | Collection[str] | None = None,
    allow_legacy_env: bool = True,
    endpoint: str,
) -> None:
    """Raise 403 unless ``identity`` is an admitted principal for ``endpoint``.

    Parameters
    ----------
    identity:
        Return value of ``verify_service_token``. ``None`` means the
        legacy env-var fallback matched (no active DB row). In the live
        stack the shared fleet secret normally resolves as
        ``service_client`` ``client_name='router-primary'`` instead.
    allowed_kinds:
        ``CallerIdentity.kind`` values admitted. Default is only
        ``service_client`` — agent-kind tokens are never admitted unless
        explicitly listed. Bare ``str`` is rejected (would iterate chars).
    allowed_client_types:
        When set, a ``service_client`` identity must also have a
        ``client_type`` in this set. ``None`` means any client_type.
        Unattributed legacy (``identity is None``) can never satisfy a
        non-None restriction — see module docstring. Bare ``str`` rejected.
    allow_legacy_env:
        Whether ``identity is None`` is admitted when
        ``allowed_client_types`` is ``None``. Must be False whenever a
        ``client_type`` restriction is in force; contradictory True is
        treated as False (fail-closed) and logged.
    endpoint:
        Route label for the 403 detail / audit (e.g.
        ``GET /api/models/router-primary``). Never a model upstream URL.
    """
    kinds = _as_str_set(allowed_kinds, param="allowed_kinds")
    client_types = (
        None
        if allowed_client_types is None
        else _as_str_set(allowed_client_types, param="allowed_client_types")
    )
    if client_types is not None and allow_legacy_env:
        logger.warning(
            "service_principal_contradictory_flags endpoint=%s "
            "allowed_client_types set but allow_legacy_env=True; "
            "forcing refuse for unattributed legacy",
            endpoint,
        )
        allow_legacy_env = False

    if identity is None:
        # client_type restriction cannot be proven for an unattributed
        # principal — fail closed so deactivating the owning DB row
        # cannot silently widen admission via the env fallback.
        if client_types is not None:
            _deny(
                db,
                endpoint=endpoint,
                reason="legacy_env_cannot_satisfy_client_type",
                detail=(
                    f"{endpoint} 要求 client_type={sorted(client_types)};"
                    "未歸屬的 legacy env token 無法證明 client_type"
                ),
                kind=None,
                client_type=None,
                service_client_id=None,
            )
        if allow_legacy_env:
            _admit_legacy(db, endpoint=endpoint)
            return
        _deny(
            db,
            endpoint=endpoint,
            reason="legacy_env_not_allowed",
            detail=(
                f"{endpoint} 不接受未歸屬的 legacy service token;"
                "請改用對應種類的 service_clients 權杖"
            ),
            kind=None,
            client_type=None,
            service_client_id=None,
        )

    if identity.kind not in kinds:
        _deny(
            db,
            endpoint=endpoint,
            reason="kind_not_admitted",
            detail=(
                f"{endpoint} 不接受 kind={identity.kind!r} 的 service token;"
                f"允許的 kind: {sorted(kinds)}"
            ),
            kind=identity.kind,
            client_type=None,
            service_client_id=identity.service_client_id,
            agent_id=identity.agent_id,
        )

    if identity.kind != "service_client" or client_types is None:
        return

    if identity.service_client_id is None:
        _deny(
            db,
            endpoint=endpoint,
            reason="missing_service_client_id",
            detail=f"{endpoint} 的 service_client 身分缺少 service_client_id",
            kind=identity.kind,
            client_type=None,
            service_client_id=None,
        )

    row = (
        db.query(ServiceClient)
        .filter(ServiceClient.id == identity.service_client_id)
        .first()
    )
    if row is None:
        _deny(
            db,
            endpoint=endpoint,
            reason="service_client_row_missing",
            detail=f"{endpoint} 找不到對應的 service_clients 列",
            kind=identity.kind,
            client_type=None,
            service_client_id=identity.service_client_id,
        )

    if row.client_type not in client_types:
        _deny(
            db,
            endpoint=endpoint,
            reason="client_type_not_admitted",
            detail=(
                f"{endpoint} 不接受 client_type={row.client_type!r};"
                f"允許的 client_type: {sorted(client_types)}"
            ),
            kind=identity.kind,
            client_type=row.client_type,
            service_client_id=identity.service_client_id,
        )


def _admit_legacy(db: Session, *, endpoint: str) -> None:
    """Record that an unattributed legacy env principal was admitted."""
    metadata = {
        "endpoint": endpoint,
        "kind": None,
        "decision": "admit_legacy_env",
    }
    logger.info(
        "service_principal_legacy_env_admitted endpoint=%s",
        endpoint,
    )
    try:
        log_audit_event(
            db,
            actor=None,
            action=AUDIT_LEGACY_ENV_ADMITTED,
            resource_type="service_principal",
            resource_id=endpoint,
            status="success",
            detail=f"{endpoint} admitted unattributed legacy env token",
            metadata=metadata,
            commit=True,
        )
    except Exception:  # noqa: BLE001 — never let audit fail admission
        logger.exception(
            "Failed to write %s audit for endpoint=%s",
            AUDIT_LEGACY_ENV_ADMITTED,
            endpoint,
        )


def _deny(
    db: Session,
    *,
    endpoint: str,
    reason: str,
    detail: str,
    kind: str | None,
    client_type: str | None,
    service_client_id: int | None,
    agent_id: int | None = None,
) -> None:
    """Persist a diagnosable denial, then raise 403.

    Metadata carries route label + principal kind only — never the
    presented token, and never a model upstream URL.
    """
    metadata = {
        "endpoint": endpoint,
        "reason": reason,
        "kind": kind,
        "client_type": client_type,
        "service_client_id": service_client_id,
        "agent_id": agent_id,
        "decision": "deny",
    }
    logger.warning(
        "service_principal_kind_denied endpoint=%s reason=%s kind=%s "
        "client_type=%s service_client_id=%s agent_id=%s",
        endpoint,
        reason,
        kind,
        client_type,
        service_client_id,
        agent_id,
    )
    try:
        log_audit_event(
            db,
            actor=None,
            action=AUDIT_KIND_DENIED,
            resource_type="service_principal",
            resource_id=endpoint,
            status="denied",
            detail=detail,
            metadata=metadata,
            commit=True,
        )
    except Exception:  # noqa: BLE001 — still deny even if audit fails
        logger.exception(
            "Failed to write %s audit for endpoint=%s",
            AUDIT_KIND_DENIED,
            endpoint,
        )
    raise HTTPException(status_code=403, detail=detail)
