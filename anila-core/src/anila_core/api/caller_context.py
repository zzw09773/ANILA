"""Per-request caller identity for agents fronted by the CSP proxy.

CSP's :func:`_build_downstream_headers` (in
``myCSPPlatform.app.services.proxy_service``) sets a small set of
``X-ANILA-*`` / ``X-CSP-*`` headers on every request it forwards to
an agent. They carry the bits an agent needs to interact with
platform services on the calling user's behalf:

* ``X-ANILA-User-Id`` — which user the agent is serving.
* ``X-ANILA-User-Email`` — convenience for audit / logging.
* ``X-CSP-Service-Token`` — the agent's own service credential
  (``csk-...``). Agent uses this to call back into CSP for
  cross-tenant reads (most notably memory) without re-doing auth.

This module captures those headers as a typed
:class:`CallerContext`. The :func:`extract_caller_context` FastAPI
dependency surfaces it from the request, falling back to ``None``
fields when a header is absent (e.g. local dev curl with no proxy
in front). Agents that opt into anila-core's reference server
(``api.server``) get the context automatically; agents using their
own framework can call :func:`extract_caller_context` directly or
construct a :class:`CallerContext` from the headers manually.

``csp_base_url`` doesn't ride on a header — agents know the
backplane URL from their deployment env (``ANILA_CSP_BASE_URL``).
We resolve it here so callers don't repeat the env-lookup boilerplate.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from fastapi import Header, Request


_DEFAULT_BASE_URL_ENV = "ANILA_CSP_BASE_URL"


@dataclass(frozen=True)
class CallerContext:
    """Bundle of per-request identity surfaced to agent code.

    All fields are optional so the dataclass remains constructable
    in dev / test scenarios where some headers are missing. Code
    that depends on a particular field MUST None-check before use.
    """

    user_id: Optional[int] = None
    user_email: Optional[str] = None
    user_groups: Optional[str] = None
    service_token: Optional[str] = None
    # Resolved from env (``ANILA_CSP_BASE_URL``) at extract time. Held
    # here so the user_memory_reader factory can construct an httpx
    # client without re-reading env on every call.
    csp_base_url: Optional[str] = None

    @property
    def has_user(self) -> bool:
        """True when there's enough identity to attribute work."""
        return self.user_id is not None

    @property
    def has_callback_credentials(self) -> bool:
        """True when the agent has what it needs to call back into CSP."""
        return (
            self.service_token is not None
            and self.csp_base_url is not None
            and self.user_id is not None
        )


def extract_caller_context(
    request: Request,
    x_anila_user_id: Optional[str] = Header(default=None, alias="X-ANILA-User-Id"),
    x_anila_user_email: Optional[str] = Header(default=None, alias="X-ANILA-User-Email"),
    x_anila_user_groups: Optional[str] = Header(default=None, alias="X-ANILA-User-Groups"),
    x_csp_service_token: Optional[str] = Header(default=None, alias="X-CSP-Service-Token"),
) -> CallerContext:
    """FastAPI dependency. Read the CSP-forwarded identity headers.

    ``request`` is taken so we can stash the resolved context on
    ``request.state.caller_context`` — code paths that don't run
    through the dependency (background tasks spawned mid-request)
    can recover it without re-parsing headers.

    Parsing rules:

    * ``X-ANILA-User-Id`` is forwarded as a string by httpx; we
      parse to int and tolerate junk by leaving ``user_id=None``
      (agents shouldn't crash because a malformed header reached
      them — they should degrade to "no user attribution").
    """
    user_id: Optional[int] = None
    if x_anila_user_id:
        try:
            user_id = int(x_anila_user_id)
        except ValueError:
            user_id = None

    csp_base_url = os.environ.get(_DEFAULT_BASE_URL_ENV)
    if csp_base_url:
        csp_base_url = csp_base_url.rstrip("/")

    ctx = CallerContext(
        user_id=user_id,
        user_email=x_anila_user_email or None,
        user_groups=x_anila_user_groups or None,
        service_token=x_csp_service_token or None,
        csp_base_url=csp_base_url,
    )
    # Stash so background tasks / context-bound subagents can
    # recover the same instance without re-parsing.
    request.state.caller_context = ctx
    return ctx
