"""Double-submit CSRF middleware for cookie-authenticated requests.

When the SPA is authenticated via the ``anila_access_token`` httpOnly
cookie, the browser auto-attaches that cookie to every same-origin
request — including cross-origin POSTs initiated from attacker pages.
That's the classic CSRF attack surface.

Mitigation (the double-submit cookie pattern):

1. On login/refresh we set a second, non-httpOnly cookie ``anila_csrf``
   with a random value. Being non-httpOnly lets our SPA's JS read it.
2. Same-origin policy prevents a cross-origin attacker page from reading
   the cookie value, so it cannot forge a matching header.
3. This middleware requires that mutating requests carrying the session
   cookie also carry ``X-CSRF-Token: <same-value>``. A mismatch or
   missing header returns 403.

Exemptions — traffic that is NOT cookie-authenticated:

- Requests presenting ``Authorization: Bearer …`` (API key OR JWT header
  flow). Browsers never auto-attach Authorization headers, so these are
  not susceptible to CSRF.
- ``/api/auth/login`` — no session yet, nothing to protect.
- ``/api/auth/oidc/*/callback`` — initiated by external IdP, not the
  browser; cookie is set *here*.

Safe methods (GET/HEAD/OPTIONS) always skip the check.
"""

from __future__ import annotations

import hmac

from anila_core.api.routing import routed_path
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

CSRF_COOKIE_NAME = "anila_csrf"
CSRF_HEADER_NAME = "x-csrf-token"  # headers are case-insensitive
ACCESS_COOKIE_NAME = "anila_access_token"

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Path prefixes where CSRF checking is skipped entirely. Keep minimal —
# everything added here is permanently CSRF-exempt.
_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/api/auth/login",
    "/api/auth/register",
    "/api/auth/providers",
    "/api/auth/oidc/",  # includes /start and /callback
    # Logout is idempotent session-end. A Vite proxy can drop expire
    # Set-Cookie and also drop ``anila_csrf`` while leaving access /
    # refresh alive. Requiring CSRF then 403s the only request that
    # can bump ``token_version``. Forcing re-login is the worst a
    # cross-site POST can do here.
    "/api/auth/logout",
    "/api/auth/refresh/logout",
    "/health",
    "/docs",
    "/openapi.json",
    "/static/",
)


def _matches_exempt_prefix(path: str) -> bool:
    """Prefix match that respects path-segment boundaries.

    Plain ``startswith`` has no boundary, so ``/api/auth/login`` would also
    exempt ``/api/auth/login-as``, ``/health`` would exempt ``/healthz``,
    and ``/openapi.json`` would exempt ``/openapi.json.bak`` — permanently
    and silently, the moment such a route is added. An entry that already
    ends in ``/`` keeps plain prefix semantics, because the trailing slash
    *is* the boundary.

    This changes nothing for the entries currently listed: every one of
    them still matches exactly the routes it matched before (pinned by
    ``test_no_unexpected_mutating_route_is_csrf_exempt``). It is a
    narrowing of what a *future* route could accidentally inherit.

    Note this is not what closed CVE-2026-48710 — a polluted ``Host`` put
    the exempt prefix at a real segment boundary, so boundary matching
    alone would still have skipped the check. That fix is
    :func:`~anila_core.api.routing.routed_path`; this is defence in depth
    against a different mistake.
    """
    for prefix in _EXEMPT_PREFIXES:
        if prefix.endswith("/"):
            if path.startswith(prefix):
                return True
        elif path == prefix or path.startswith(prefix + "/"):
            return True
    return False


class CsrfMiddleware(BaseHTTPMiddleware):
    """Enforce double-submit CSRF on cookie-authenticated mutating requests."""

    async def dispatch(self, request: Request, call_next) -> Response:
        if _should_skip(request):
            return await call_next(request)

        cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
        header_token = request.headers.get(CSRF_HEADER_NAME)

        # constant-time 比對，避免透露 token 前綴。雖然 CSRF token 是短期
        # 隨機值，仍依 OWASP 建議使用 hmac.compare_digest。
        if (
            not cookie_token
            or not header_token
            or not hmac.compare_digest(cookie_token, header_token)
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": "CSRF 驗證失敗，請重新登入"},
            )

        return await call_next(request)


def _should_skip(request: Request) -> bool:
    method = request.method.upper()
    if method in SAFE_METHODS:
        return True

    # SECURITY: the exemption is decided on the path the router dispatches
    # on, never on ``request.url.path`` — see ``routed_path``'s docstring
    # for why (CVE-2026-48710: ``request.url`` is built from the caller's
    # ``Host`` header, so it can be made to report an exempt prefix while
    # the router still reaches the real endpoint).
    if _matches_exempt_prefix(routed_path(request)):
        return True

    # Bearer-authenticated requests are not cookie-authenticated and thus
    # not susceptible to CSRF. The middleware trusts the presence of
    # an Authorization header as a signal of non-browser-initiated (or
    # at least non-cookie-dependent) auth, same as standard practice.
    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        return True

    # No session cookie → nothing to hijack. Let downstream auth
    # dependency return the appropriate 401.
    if not request.cookies.get(ACCESS_COOKIE_NAME):
        return True

    return False
