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
    "/health",
    "/docs",
    "/openapi.json",
    "/static/",
)


class CsrfMiddleware(BaseHTTPMiddleware):
    """Enforce double-submit CSRF on cookie-authenticated mutating requests."""

    async def dispatch(self, request: Request, call_next) -> Response:
        if _should_skip(request):
            return await call_next(request)

        cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
        header_token = request.headers.get(CSRF_HEADER_NAME)

        if not cookie_token or not header_token or cookie_token != header_token:
            return JSONResponse(
                status_code=403,
                content={"detail": "CSRF 驗證失敗，請重新登入"},
            )

        return await call_next(request)


def _should_skip(request: Request) -> bool:
    method = request.method.upper()
    if method in SAFE_METHODS:
        return True

    path = request.url.path
    for prefix in _EXEMPT_PREFIXES:
        if path.startswith(prefix):
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
