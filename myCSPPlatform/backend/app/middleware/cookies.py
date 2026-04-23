"""Helpers for setting / clearing the SPA's session cookies.

Three cookies make up the Wave 2 session:

- ``anila_access_token``  — httpOnly, short-lived JWT bearer. Read by
  ``get_caller`` and ``get_current_user`` as a fallback source when no
  ``Authorization`` header is present.
- ``anila_refresh_token`` — httpOnly, longer-lived JWT. ``Path`` is
  scoped to ``/api/auth/refresh`` so it never leaks into other
  endpoints' request context.
- ``anila_csrf``          — NOT httpOnly; the SPA's JS reads it and
  echoes the value back as the ``X-CSRF-Token`` header on mutating
  requests (double-submit pattern, see ``middleware/csrf.py``).

``samesite="lax"`` is used instead of ``strict`` because OIDC callbacks
land from the IdP's origin (top-level navigation) and Strict would
suppress the cookies on that initial arrival — breaking SSO. Lax still
blocks the scariest CSRF shapes (cross-site POST) and the explicit CSRF
token layer covers the rest.
"""

from __future__ import annotations

import secrets

from fastapi import Response

from app.config import settings

ACCESS_COOKIE_NAME = "anila_access_token"
REFRESH_COOKIE_NAME = "anila_refresh_token"
CSRF_COOKIE_NAME = "anila_csrf"
REFRESH_COOKIE_PATH = "/api/auth/refresh"


def _cookie_secure() -> bool:
    """Cookies are Secure (HTTPS-only) unless explicitly opted out via
    ``COOKIE_SECURE=false`` — needed for the TestClient and for bare
    local dev loops that have no TLS-terminating reverse proxy."""
    return bool(settings.COOKIE_SECURE)


def set_session_cookies(
    response: Response, *, access_token: str, refresh_token: str
) -> str:
    """Attach access / refresh / csrf cookies to ``response``.

    Returns the newly-minted CSRF token so the caller can also echo it
    in a JSON body when useful (e.g. the SPA's bootstrap path can read
    it synchronously rather than waiting for a second request).
    """
    access_max_age = int(settings.ACCESS_TOKEN_EXPIRE_MINUTES) * 60
    refresh_max_age = int(settings.REFRESH_TOKEN_EXPIRE_DAYS) * 86400

    response.set_cookie(
        ACCESS_COOKIE_NAME,
        access_token,
        max_age=access_max_age,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        refresh_token,
        max_age=refresh_max_age,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path=REFRESH_COOKIE_PATH,
    )

    csrf_token = secrets.token_urlsafe(32)
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        max_age=access_max_age,
        httponly=False,  # SPA must read this
        secure=_cookie_secure(),
        samesite="lax",
        path="/",
    )
    return csrf_token


def clear_session_cookies(response: Response) -> None:
    """Remove all session cookies — used on logout and on refresh failure."""
    for name, path in (
        (ACCESS_COOKIE_NAME, "/"),
        (REFRESH_COOKIE_NAME, REFRESH_COOKIE_PATH),
        (CSRF_COOKIE_NAME, "/"),
    ):
        response.delete_cookie(name, path=path)
