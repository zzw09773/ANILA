"""OIDC login flow: provider listing, /oidc/{id}/start, /oidc/{id}/callback.

Split from the original ``app/api/auth.py`` god-module — bodies moved
verbatim; only this import header is new.
"""
from datetime import datetime, timedelta, timezone
from html import escape

from fastapi import Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.middleware.cookies import set_session_cookies
from app.models.api_key import ApiKey
from app.models.auth_provider import AuthProvider
from app.models.model_registry import ModelRegistry
from app.models.user import User
from app.schemas.auth_provider import PublicAuthProviderResponse
from app.services.api_key_service import create_api_key
from app.services.audit_service import log_audit_event
from app.services.auth_service import create_tokens
from app.services.external_auth_service import (
    authenticate_oidc_code,
    build_oidc_authorization_url,
    decode_external_state,
    list_public_auth_providers,
    sanitize_next_path,
)

from ._common import _reject_when_card_only, _stamp_last_login, router


def _mint_sso_api_key(db: Session, user: User) -> str | None:
    """Mint a short-lived (24h) API Key bound to all currently-active models so
    the SPA can immediately call /v1/* after SSO without forcing the user to
    paste a key. Returns the raw key, or None if no active models exist (in
    which case the SPA falls back to its API-Key popover).

    Any prior ``sso-*`` keys for the same user are revoked first so the DB
    does not accumulate orphan keys across repeated OIDC round-trips. The
    raw key is only delivered once (hand-off through the HTML callback) so
    old rows can never be recovered by the SPA anyway.
    """
    model_ids = [
        row.id for row in db.query(ModelRegistry).filter(ModelRegistry.is_active == True).all()
    ]
    if not model_ids:
        return None

    # Revoke previously-minted SSO keys for this user. `sso-*` is a naming
    # convention owned entirely by this function — user-named keys (CLI
    # tokens, etc.) remain active.
    (
        db.query(ApiKey)
        .filter(
            ApiKey.user_id == user.id,
            ApiKey.name.like("sso-%"),
            ApiKey.is_active == True,  # noqa: E712
        )
        .update({"is_active": False}, synchronize_session=False)
    )

    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    _, raw_key = create_api_key(
        db,
        user_id=user.id,
        name=f"sso-{user.username}-{int(expires_at.timestamp())}",
        model_ids=model_ids,
        expires_at=expires_at,
    )
    return raw_key


def _build_oidc_callback_html(
    tokens: dict, next_path: str, api_key: str | None = None
) -> HTMLResponse:
    """Render the tiny HTML page that finalizes an OIDC round-trip.

    Wave 2: tokens are delivered via ``Set-Cookie`` on this response, not
    injected into ``localStorage`` / ``sessionStorage``. The HTML's only
    job is to redirect the browser back to the SPA's next_path. We keep
    ``tokens`` / ``api_key`` parameters in the signature for transitional
    compatibility with any callers still constructing the response
    manually; neither is embedded in the HTML anymore.
    """
    del tokens, api_key  # no longer embedded — cookies do the handoff
    # B3 縱深：state 端與 query 端都 sanitize 過一次，這裡再 sanitize 一次，
    # 然後 HTML escape — 三層任何一層通過都安全。
    safe_next = escape(sanitize_next_path(next_path))
    body = f"""
<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="0; url={safe_next}">
  <title>登入完成</title>
</head>
<body>
<script>window.location.replace('{safe_next}');</script>
正在完成登入...
</body>
</html>
"""
    return HTMLResponse(body)


@router.get("/providers", response_model=list[PublicAuthProviderResponse])
def public_providers(db: Session = Depends(get_db)):
    providers = list_public_auth_providers(db)
    # Branch SSO：強制卡片登入時，不在 /providers 列出 OIDC providers，
    # 讓 SPA 自然不顯示對應的 tab。已建立的 OIDC provider row 不刪除（admin
    # 切回非鎖死模式時應該還能用）；純粹在邊界 hide 掉。
    if settings.REQUIRE_CARD_LOGIN_ONLY:
        providers = [p for p in providers if p.provider_type != "oidc"]
    return [
        {
            "id": provider.id,
            "name": provider.name,
            "provider_type": provider.provider_type,
            "button_text": provider.button_text,
        }
        for provider in providers
    ]


@router.get("/oidc/{provider_id}/start")
async def start_oidc_login(
    provider_id: int,
    next_path: str = "/",
    db: Session = Depends(get_db),
):
    _reject_when_card_only()
    provider = (
        db.query(AuthProvider)
        .filter(
            AuthProvider.id == provider_id,
            AuthProvider.provider_type == "oidc",
            AuthProvider.is_active == True,
        )
        .first()
    )
    if not provider:
        raise HTTPException(status_code=404, detail="OIDC Provider 不存在")
    authorization_url = await build_oidc_authorization_url(
        provider, next_path=sanitize_next_path(next_path),
    )
    return {"authorization_url": authorization_url}


@router.get("/oidc/{provider_id}/callback", include_in_schema=False)
async def oidc_callback(
    provider_id: int,
    code: str,
    state: str,
    db: Session = Depends(get_db),
):
    _reject_when_card_only()
    provider = (
        db.query(AuthProvider)
        .filter(
            AuthProvider.id == provider_id,
            AuthProvider.provider_type == "oidc",
            AuthProvider.is_active == True,
        )
        .first()
    )
    if not provider:
        return HTMLResponse("OIDC Provider 不存在", status_code=404)

    try:
        state_payload = decode_external_state(state)
        if int(state_payload["provider_id"]) != provider_id:
            raise ValueError("state provider 不一致")
        # state 內有 PKCE verifier 與 nonce，必須完整傳給 authenticate_oidc_code
        # 才能驗 id_token；任何缺漏由該函式 raise ValueError。
        user = await authenticate_oidc_code(db, provider, code, state_payload)
        tokens = create_tokens(user, amr=("oidc",))
        _stamp_last_login(db, user)
        log_audit_event(
            db,
            actor=user,
            action="login",
            resource_type="auth",
            resource_id=user.id,
            detail=f"OIDC 登入成功: {provider.name}",
            commit=True,
        )
        html = _build_oidc_callback_html(
            tokens,
            state_payload.get("next_path", "/"),
        )
        # Cookies carry the session — SPA reads `__Host-anila_csrf` (non-httpOnly)
        # on first render and echoes it on mutating requests.
        set_session_cookies(
            html,
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
        )
        return html
    except Exception as exc:
        log_audit_event(
            db,
            action="login",
            resource_type="auth",
            status="failure",
            detail=f"OIDC 登入失敗: {provider.name} ({exc})",
            commit=True,
        )
        return HTMLResponse(
            f"<html><body><h3>OIDC 登入失敗</h3><p>{escape(str(exc))}</p><a href='/login'>返回登入頁</a></body></html>",
            status_code=400,
        )
