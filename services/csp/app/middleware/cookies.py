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

SameSite policy 條件式選擇 (見 ``_cookie_samesite``):
- **card-only mode** (``ANILA_AUTH_MODE=card-only``,內網 prod):升 ``Strict``。
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
from sqlalchemy.orm import Session

from app.config import settings

ACCESS_COOKIE_NAME = "anila_access_token"
REFRESH_COOKIE_NAME = "anila_refresh_token"
CSRF_COOKIE_NAME = "anila_csrf"
REFRESH_COOKIE_PATH = "/api/auth/refresh"


def _cookie_secure() -> bool:
    """All deployed entry points are HTTPS; tests replace this helper."""
    return True


def _cookie_samesite() -> str:
    """``ANILA_AUTH_MODE=card-only`` 沒有 OIDC top-level callback 需求,
    可升 SameSite=Strict 收緊 CSRF 防線;非 card-only mode (含 OIDC SSO)
    維持 Lax 讓 IdP redirect 能帶回 cookie。
    """
    return "strict" if settings.ANILA_AUTH_MODE == "card-only" else "lax"


def set_session_cookies(
    response: Response,
    *,
    access_token: str,
    refresh_token: str,
    db: Session | None = None,
    token_lifetimes: tuple[int, int] | None = None,
) -> str:
    """Attach access / refresh / csrf cookies to ``response``.

    Returns the newly-minted CSRF token so the caller can also echo it
    in a JSON body when useful (e.g. the SPA's bootstrap path can read
    it synchronously rather than waiting for a second request).
    """
    from app.models.platform_setting import get_setting

    if token_lifetimes is not None:
        access_minutes, refresh_days = token_lifetimes
    elif db is None:
        from app.database import SessionLocal

        with SessionLocal() as session:
            access_minutes = int(
                get_setting(session, "auth.access_token_expire_minutes")
            )
            refresh_days = int(get_setting(session, "auth.refresh_token_expire_days"))
    else:
        access_minutes = int(get_setting(db, "auth.access_token_expire_minutes"))
        refresh_days = int(get_setting(db, "auth.refresh_token_expire_days"))
    access_max_age = access_minutes * 60
    refresh_max_age = refresh_days * 86400

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
    for name, path in (
        (ACCESS_COOKIE_NAME, "/"),
        (REFRESH_COOKIE_NAME, REFRESH_COOKIE_PATH),
        (CSRF_COOKIE_NAME, "/"),
    ):
        response.delete_cookie(name, path=path)
