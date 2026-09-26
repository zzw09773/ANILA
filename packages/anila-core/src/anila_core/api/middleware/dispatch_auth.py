"""JWKS-verifying middleware for CSP short-lived dispatch identity (P2.1).

Replaces the role of ``CspServiceTokenMiddleware`` for agent inbound auth:

* CSP sends ``Authorization: Bearer <RS256 JWT>`` (5-minute TTL).
* This middleware verifies against ``{csp_base_url}/.well-known/jwks.json``.
* Verified claims land on ``request.state.anila_dispatch``.

FAIL-CLOSED: blank / missing JWKS configuration rejects non-public
requests. The legacy hole (empty ``service_token`` ⇒ pass-through) must
not exist here.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from ..routing import routed_path
from .dispatch_jwt import (
    DISPATCH_TOKEN_AUDIENCE,
    DISPATCH_TOKEN_ISSUER,
    DispatchTokenError,
    extract_bearer,
    parse_unverified_header,
    verify_dispatch_jwt,
)
from .jwks_client import (
    JwksClient,
    JwksError,
    JwksFetchError,
    JwksKeyNotFoundError,
    derive_jwks_url,
)


logger = logging.getLogger(__name__)

# Tested against ``routed_path(request)``, never ``request.url.path`` —
# the latter is assembled from the caller's ``Host`` header, so it is not
# the string the router dispatches on.
_PUBLIC_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}

# Env already used by agents for the platform base URL — prefer over new knobs.
_ENV_CSP_BASE = "CSP_BASE_URL"
# Explicit CA bundle file (CSPKI). Never SSL_CERT_FILE.
_ENV_CA_FILE = "ANILA_CA_FILE"


class DispatchIdentityMiddleware(BaseHTTPMiddleware):
    """Verify CSP dispatch JWT via JWKS; expose claims on ``request.state``.

    Constructor knobs (only what pointing at the platform requires):

    * ``csp_base_url`` / ``jwks_url`` — JWKS URL is derived from base when
      omitted (``{base}/.well-known/jwks.json``).
    * ``ca_file`` — PEM CA bundle path for the JWKS HTTPS fetch.

    ``dev_mode=True`` is an *explicit* local-dev bypass (same role as the
    old middleware's flag). Blank configuration with ``dev_mode=False``
    rejects — it does **not** open the door.
    """

    def __init__(
        self,
        app: Any,
        *,
        csp_base_url: str | None = None,
        jwks_url: str | None = None,
        ca_file: str | None = None,
        jwks_client: JwksClient | None = None,
        dev_mode: bool = False,
        issuer: str = DISPATCH_TOKEN_ISSUER,
        audience: str = DISPATCH_TOKEN_AUDIENCE,
    ) -> None:
        super().__init__(app)
        self._dev_mode = bool(dev_mode)
        self._issuer = issuer
        self._audience = audience

        if jwks_client is not None:
            self._jwks = jwks_client
        else:
            resolved_jwks = (jwks_url or "").strip()
            if not resolved_jwks:
                base = (csp_base_url if csp_base_url is not None else os.environ.get(
                    _ENV_CSP_BASE, ""
                ))
                resolved_jwks = derive_jwks_url(base or "")
            resolved_ca = ca_file
            if resolved_ca is None:
                resolved_ca = (os.environ.get(_ENV_CA_FILE) or "").strip() or None
            self._jwks = JwksClient(resolved_jwks, ca_file=resolved_ca)

        if not self._jwks.configured and not self._dev_mode:
            logger.error(
                "DispatchIdentityMiddleware: JWKS URL unset — every non-public "
                "request will be REJECTED (fail-closed)."
            )

    @property
    def jwks_client(self) -> JwksClient:
        return self._jwks

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        if self._dev_mode:
            return await call_next(request)

        if routed_path(request) in _PUBLIC_PATHS:
            return await call_next(request)

        if not self._jwks.configured:
            return JSONResponse(
                status_code=401,
                content={"detail": "dispatch auth misconfigured: JWKS URL unset"},
            )

        try:
            token = extract_bearer(request.headers.get("Authorization"))
            header = parse_unverified_header(token)
            kid = header.get("kid")
            if not kid or not isinstance(kid, str):
                raise DispatchTokenError("missing kid")
            public_key = await self._jwks.get_public_key(kid)
            claims = verify_dispatch_jwt(
                token,
                public_key,
                issuer=self._issuer,
                audience=self._audience,
            )
            if not self._jwks.allows_issuance(kid, claims):
                raise DispatchTokenError("signing key is outside its issuance window")
        except JwksFetchError as exc:
            logger.error("JWKS fetch failed during dispatch verify: %s", exc)
            return JSONResponse(
                status_code=503,
                content={"detail": "auth keys unavailable"},
            )
        except JwksKeyNotFoundError as exc:
            logger.warning("dispatch JWT kid not in JWKS: %s", exc)
            return JSONResponse(
                status_code=401,
                content={"detail": "invalid dispatch token"},
            )
        except (DispatchTokenError, JwksError) as exc:
            logger.debug("dispatch JWT rejected: %s", exc)
            return JSONResponse(
                status_code=401,
                content={"detail": "invalid or missing dispatch token"},
            )

        request.state.anila_dispatch = claims
        return await call_next(request)


def claims_from_request(request: Request) -> Optional[dict[str, Any]]:
    """Return verified dispatch claims, or ``None`` if absent."""
    return getattr(request.state, "anila_dispatch", None)


__all__ = [
    "DispatchIdentityMiddleware",
    "claims_from_request",
]
