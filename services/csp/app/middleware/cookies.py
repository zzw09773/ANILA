"""Helpers for setting / clearing the SPA's session cookies.

Three cookies make up the browser session. Formal HTTPS deployments use the
``__Host-`` prefix, which browsers accept only when ``Secure`` is present,
``Path=/``, and no ``Domain`` attribute is set:

- ``__Host-anila_access_token``  — httpOnly, short-lived JWT bearer. Read by
  ``get_caller`` and ``get_current_user`` as a fallback source when no
  ``Authorization`` header is present.
- ``__Host-anila_refresh_token`` — httpOnly, longer-lived JWT. The ``__Host-``
  invariant requires ``Path=/``; consumers still reject it anywhere an access
  token is required by checking the JWT ``type`` claim.
- ``__Host-anila_csrf``          — NOT httpOnly; the SPA's JS reads it and
  echoes the value back as the ``X-CSRF-Token`` header on mutating
  requests (double-submit pattern, see ``middleware/csrf.py``).

HTTP-only unit/local-dev runs cannot use a standards-compliant ``__Host-``
cookie. When and only when ``COOKIE_SECURE=false``, all three names switch to
the visibly non-production ``anila_dev_*`` namespace. The former unprefixed
``anila_*`` cookies are never accepted as a fallback.

SameSite policy 條件式選擇 (見 ``_cookie_samesite``):
- **card-only mode** (``REQUIRE_CARD_LOGIN_ONLY=true``,內網 prod):升 ``Strict``。
  這個模式下沒有 OIDC top-level callback (endpoint 已 lockdown 404),Strict 不
  會 break 任何 flow,反而連 cross-site GET navigation 都不帶 cookie,徹底擋
  掉 CSRF surface。
- **其他模式** (含 OIDC SSO / 本機帳密):維持 ``Lax``。OIDC callback 從 IdP
  origin 經 top-level navigation 進來,Strict 會 suppress cookie 害 SSO 壞掉。
  Lax 仍擋住 cross-site POST 這類最危險形狀,雙保險靠 CSRF token 那層。
"""

from __future__ import annotations

import secrets

from fastapi import Response

from app.config import settings

SECURE_ACCESS_COOKIE_NAME = "__Host-anila_access_token"
SECURE_REFRESH_COOKIE_NAME = "__Host-anila_refresh_token"
SECURE_CSRF_COOKIE_NAME = "__Host-anila_csrf"

DEV_ACCESS_COOKIE_NAME = "anila_dev_access_token"
DEV_REFRESH_COOKIE_NAME = "anila_dev_refresh_token"
DEV_CSRF_COOKIE_NAME = "anila_dev_csrf"


def cookie_names(secure: bool) -> tuple[str, str, str]:
    """Return access/refresh/CSRF names for one explicit security profile."""
    if secure:
        return (
            SECURE_ACCESS_COOKIE_NAME,
            SECURE_REFRESH_COOKIE_NAME,
            SECURE_CSRF_COOKIE_NAME,
        )
    return DEV_ACCESS_COOKIE_NAME, DEV_REFRESH_COOKIE_NAME, DEV_CSRF_COOKIE_NAME


ACCESS_COOKIE_NAME, REFRESH_COOKIE_NAME, CSRF_COOKIE_NAME = cookie_names(
    bool(settings.COOKIE_SECURE)
)

# ``__Host-`` cookies must always use the root path. Keep the same path in the
# explicit dev profile so auth behavior does not diverge between environments.
REFRESH_COOKIE_PATH = "/"


def _cookie_secure() -> bool:
    """Cookies are Secure (HTTPS-only) unless explicitly opted out via
    ``COOKIE_SECURE=false`` — needed for the TestClient and for bare
    local dev loops that have no TLS-terminating reverse proxy."""
    return bool(settings.COOKIE_SECURE)


def _cookie_samesite() -> str:
    """``REQUIRE_CARD_LOGIN_ONLY`` 模式沒有 OIDC top-level callback 需求,
    可升 SameSite=Strict 收緊 CSRF 防線;非 card-only mode (含 OIDC SSO)
    維持 Lax 讓 IdP redirect 能帶回 cookie。
    """
    return "strict" if settings.REQUIRE_CARD_LOGIN_ONLY else "lax"


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
        samesite=_cookie_samesite(),
        path="/",
    )
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        refresh_token,
        max_age=refresh_max_age,
        httponly=True,
        secure=_cookie_secure(),
        samesite=_cookie_samesite(),
        path=REFRESH_COOKIE_PATH,
    )

    csrf_token = secrets.token_urlsafe(32)
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        max_age=access_max_age,
        httponly=False,  # SPA must read this
        secure=_cookie_secure(),
        samesite=_cookie_samesite(),
        path="/",
    )
    return csrf_token


def clear_session_cookies(response: Response) -> None:
    """Remove all session cookies — used on logout and on refresh failure."""
    for name, httponly in (
        (ACCESS_COOKIE_NAME, True),
        (REFRESH_COOKIE_NAME, True),
        (CSRF_COOKIE_NAME, False),
    ):
        # A __Host- deletion is itself a Set-Cookie operation and must retain
        # Secure + Path=/ or a conforming browser will ignore it.
        response.delete_cookie(
            name,
            path="/",
            secure=_cookie_secure(),
            httponly=httponly,
            samesite=_cookie_samesite(),
        )
