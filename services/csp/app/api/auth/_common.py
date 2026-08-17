"""Shared router + cross-submodule helpers for the auth package.

Split from the original ``app/api/auth.py`` god-module (837L) — bodies moved
verbatim; only this import header is new.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Response
from sqlalchemy.orm import Session

from app.config import settings
from app.middleware.cookies import set_session_cookies
from app.models.user import User
from app.services.auth_service import TOKEN_LIFETIMES_KEY


router = APIRouter(prefix="/api/auth", tags=["認證"])


def _require_card_login_enabled() -> None:
    """Endpoint guard：auth mode 未啟用卡片登入時假裝 endpoint 不存在。

    Pattern 對齊 OIDC：未啟用時回 404 而非 403，避免暴露功能 existence。
    """
    if settings.ANILA_AUTH_MODE not in {"mixed", "card-only"}:
        raise HTTPException(status_code=404)


def _reject_when_card_only() -> None:
    """Branch SSO lockdown：``ANILA_AUTH_MODE=card-only`` 時封閉非卡片登入路徑。

    回 404 而非 403，避免直接透露政策狀態；但這不等於完整不可區分：
    不同方法與請求內容仍可產生可觀察差異（例如 GET 的 HTML/JSON、
    POST 的 405/422/404）。不要據此推論 endpoint 無法被列舉。

    註:``/login`` 與 ``PUT /password`` 不走這個 helper — 它們有 **owner
    例外**(break-glass 帳密通道,2026-06-11),gate 寫在各自端點內,
    失敗姿態同樣是 404。
    """
    if settings.ANILA_AUTH_MODE == "card-only":
        raise HTTPException(status_code=404)


def _finalize_login(response: Response, tokens: dict, db: Session) -> dict:
    """Attach session cookies to the response and surface the CSRF token
    in the JSON body so the SPA can read it even on its first request."""
    token_lifetimes = tokens.pop(TOKEN_LIFETIMES_KEY, None)
    csrf = set_session_cookies(
        response,
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        db=db,
        token_lifetimes=token_lifetimes,
    )
    return {**tokens, "csrf_token": csrf}


def _stamp_last_login(db: Session, user: User) -> None:
    """Record the current timestamp on the user's profile. Called on every
    successful login path (local, LDAP, OIDC) so the admin user panel can
    show ``last_login_at`` without scanning the audit log."""
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
