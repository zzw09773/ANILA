"""Per-request caller identity for agents fronted by the CSP proxy.

P2.1: inbound identity is the **verified dispatch JWT**
(``request.state.anila_dispatch`` via :class:`DispatchIdentityMiddleware`).
Plaintext ``X-ANILA-User-Id`` / ``-Email`` / ``-Groups`` headers are no
longer a trusted identity source.

:func:`extract_caller_context` previously read those plaintext headers as
a FastAPI dependency — that path is retired and now raises so agent
authors cannot accidentally re-introduce header-trusted identity.

Construct :class:`CallerContext` from verified claims (or tests) explicitly::

    from anila_core.api.middleware import claims_from_request
    claims = claims_from_request(request) or {}
    ctx = CallerContext(
        user_id=str(claims["user_id"]) if claims.get("user_id") is not None else None,
        ...
    )
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from fastapi import Header, Request


_DEFAULT_BASE_URL_ENV = "ANILA_CSP_BASE_URL"

_PLAINTEXT_IDENTITY_RETIRED = (
    "extract_caller_context is retired (P2.1): plaintext X-ANILA-User-* "
    "headers are no longer trusted for identity. Use claims_from_request("
    "request) after DispatchIdentityMiddleware, or construct CallerContext "
    "from verified JWT claims."
)


@dataclass(frozen=True)
class CallerContext:
    """Bundle of per-request identity surfaced to agent code.

    All fields are optional so the dataclass remains constructable
    in dev / test scenarios where some headers are missing. Code
    that depends on a particular field MUST None-check before use.
    """

    user_id: Optional[str] = None
    user_email: Optional[str] = None
    user_groups: Optional[str] = None
    service_token: Optional[str] = None
    # Resolved from env (``ANILA_CSP_BASE_URL``) at extract time. Held
    # here so the user_memory_reader factory can construct an httpx
    # client without re-reading env on every call.
    csp_base_url: Optional[str] = None
    # Full-Trace Protocol correlation (doc-05 §5/§6). CSP forwards these
    # inbound so the router/agent can attribute spans to the originating
    # task + trace. Optional — absent on non-traced (legacy) requests.
    trace_id: Optional[str] = None
    task_id: Optional[str] = None

    @property
    def has_user(self) -> bool:
        """True when there's enough identity to attribute work."""
        return bool((self.user_id or "").strip())

    @property
    def has_callback_credentials(self) -> bool:
        """True when the agent has what it needs to call back into CSP.

        All three fields use stripped truthiness so a blank / whitespace
        header (e.g. ``X-CSP-Service-Token: ``) can't slip a half-built
        reader past the gate.
        """
        return (
            bool((self.service_token or "").strip())
            and bool((self.csp_base_url or "").strip())
            and bool((self.user_id or "").strip())
        )


def caller_context_from_dispatch(
    request: Request,
    *,
    service_token: Optional[str] = None,
) -> CallerContext:
    """Build CallerContext from verified dispatch claims on ``request.state``.

    Trace / task ids remain readable from correlation headers (not identity).
    """
    claims = getattr(request.state, "anila_dispatch", None) or {}
    uid = claims.get("user_id")
    csp_base_url = os.environ.get(_DEFAULT_BASE_URL_ENV)
    if csp_base_url:
        csp_base_url = csp_base_url.rstrip("/")
    ctx = CallerContext(
        user_id=str(uid) if uid is not None else None,
        user_email=None,
        user_groups=None,
        service_token=service_token,
        csp_base_url=csp_base_url,
        trace_id=(request.headers.get("X-ANILA-Trace-Id") or "").strip() or None,
        task_id=(request.headers.get("X-ANILA-Task-Id") or "").strip() or None,
    )
    request.state.caller_context = ctx
    return ctx


def extract_caller_context(
    request: Request,
    x_anila_user_id: Optional[str] = Header(default=None, alias="X-ANILA-User-Id"),
    x_anila_user_email: Optional[str] = Header(default=None, alias="X-ANILA-User-Email"),
    x_anila_user_groups: Optional[str] = Header(default=None, alias="X-ANILA-User-Groups"),
    x_csp_service_token: Optional[str] = Header(default=None, alias="X-CSP-Service-Token"),
    x_anila_trace_id: Optional[str] = Header(default=None, alias="X-ANILA-Trace-Id"),
    x_anila_task_id: Optional[str] = Header(default=None, alias="X-ANILA-Task-Id"),
) -> CallerContext:
    """Retired FastAPI dependency — always raises.

    Plaintext ``X-ANILA-User-*`` identity is no longer trusted (P2.1).
    Use :func:`caller_context_from_dispatch` or build :class:`CallerContext`
    from verified JWT claims.
    """
    del (
        request,
        x_anila_user_id,
        x_anila_user_email,
        x_anila_user_groups,
        x_csp_service_token,
        x_anila_trace_id,
        x_anila_task_id,
    )
    raise RuntimeError(_PLAINTEXT_IDENTITY_RETIRED)
