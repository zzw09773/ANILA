from datetime import datetime, timedelta, timezone
from html import escape
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.auth_provider import AuthProvider
from app.models.model_registry import ModelRegistry
from app.models.user import User
from app.schemas.user import (
    LoginRequest,
    TokenResponse,
    RefreshRequest,
    PasswordChangeRequest,
    UserResponse,
    RegisterRequest,
)
from app.schemas.auth_provider import PublicAuthProviderResponse
from app.services.api_key_service import create_api_key
from app.services.audit_service import log_audit_event
from app.services.auth_service import (
    authenticate_user,
    create_tokens,
    get_current_user,
    _load_user_from_payload,
    PENDING_APPROVAL_SENTINEL,
)
from app.services.external_auth_service import (
    authenticate_ldap,
    authenticate_oidc_code,
    build_oidc_authorization_url,
    decode_external_state,
    list_public_auth_providers,
)
from app.utils.security import decode_token, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["認證"])


def _mint_sso_api_key(db: Session, user: User) -> str | None:
    """Mint a short-lived (24h) API Key bound to all currently-active models so
    the SPA can immediately call /v1/* after SSO without forcing the user to
    paste a key. Returns the raw key, or None if no active models exist (in
    which case the SPA falls back to its API-Key popover)."""
    model_ids = [
        row.id for row in db.query(ModelRegistry).filter(ModelRegistry.is_active == True).all()
    ]
    if not model_ids:
        return None
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
    safe_next = escape(next_path if next_path.startswith("/") else "/")
    safe_access = escape(tokens["access_token"])
    safe_refresh = escape(tokens["refresh_token"])
    api_key_script = (
        f"sessionStorage.setItem('anilaRuntimeApiKey', '{escape(api_key)}');"
        if api_key
        else ""
    )
    body = f"""
<!doctype html>
<html lang="zh-Hant">
<head><meta charset="utf-8"><title>登入完成</title></head>
<body>
<script>
localStorage.setItem('accessToken', '{safe_access}');
localStorage.setItem('refreshToken', '{safe_refresh}');
{api_key_script}
window.location.replace('{safe_next}');
</script>
正在完成登入...
</body>
</html>
"""
    return HTMLResponse(body)


@router.get("/providers", response_model=list[PublicAuthProviderResponse])
def public_providers(db: Session = Depends(get_db)):
    return [
        {
            "id": provider.id,
            "name": provider.name,
            "provider_type": provider.provider_type,
            "button_text": provider.button_text,
        }
        for provider in list_public_auth_providers(db)
    ]


@router.get("/oidc/{provider_id}/start")
async def start_oidc_login(
    provider_id: int,
    next_path: str = "/",
    db: Session = Depends(get_db),
):
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
    authorization_url = await build_oidc_authorization_url(provider, next_path=next_path)
    return {"authorization_url": authorization_url}


@router.post("/register", status_code=201)
def register(request: RegisterRequest, http_request: Request, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.username == request.username).first()
    if existing:
        raise HTTPException(status_code=400, detail="帳號已被使用")
    user = User(
        username=request.username,
        email=request.email,
        hashed_password=hash_password(request.password),
        role="user",
        is_active=True,
        is_approved=False,
    )
    db.add(user)
    db.commit()
    log_audit_event(
        db,
        action="register",
        resource_type="auth",
        actor=user,
        resource_id=user.id,
        detail="使用者送出註冊申請",
        ip_address=http_request.client.host if http_request.client else None,
        commit=True,
    )
    return {"message": "註冊成功，請等待管理員核准後再登入"}


@router.post("/login", response_model=TokenResponse)
def login(request: LoginRequest, http_request: Request, db: Session = Depends(get_db)):
    ip_address = http_request.client.host if http_request.client else None

    if request.auth_source == "ldap":
        provider = None
        if request.provider_id is not None:
            provider = (
                db.query(AuthProvider)
                .filter(
                    AuthProvider.id == request.provider_id,
                    AuthProvider.provider_type == "ldap",
                    AuthProvider.is_active == True,
                )
                .first()
            )
        if not provider:
            raise HTTPException(status_code=400, detail="LDAP Provider 不存在或未啟用")
        result = authenticate_ldap(db, provider, request.username, request.password)
        if result is None:
            log_audit_event(
                db,
                action="login",
                resource_type="auth",
                status="failure",
                detail=f"LDAP 登入失敗: {provider.name}/{request.username}",
                ip_address=ip_address,
                commit=True,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="LDAP 帳號或密碼錯誤",
            )
        tokens = create_tokens(result)
        log_audit_event(
            db,
            actor=result,
            action="login",
            resource_type="auth",
            resource_id=result.id,
            detail=f"LDAP 登入成功: {provider.name}",
            ip_address=ip_address,
            commit=True,
        )
        return tokens

    result = authenticate_user(db, request.username, request.password)
    if result is None:
        log_audit_event(
            db,
            action="login",
            resource_type="auth",
            status="failure",
            detail=f"本機登入失敗: {request.username}",
            ip_address=ip_address,
            commit=True,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="帳號或密碼錯誤",
        )
    if result is PENDING_APPROVAL_SENTINEL:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="等待核准中，請通知 admin",
        )
    tokens = create_tokens(result)
    log_audit_event(
        db,
        actor=result,
        action="login",
        resource_type="auth",
        resource_id=result.id,
        detail="本機登入成功",
        ip_address=ip_address,
        commit=True,
    )
    return tokens


@router.get("/oidc/{provider_id}/callback", include_in_schema=False)
async def oidc_callback(
    provider_id: int,
    code: str,
    state: str,
    db: Session = Depends(get_db),
):
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
        user = await authenticate_oidc_code(db, provider, code)
        tokens = create_tokens(user)
        sso_api_key = _mint_sso_api_key(db, user)
        log_audit_event(
            db,
            actor=user,
            action="login",
            resource_type="auth",
            resource_id=user.id,
            detail=(
                f"OIDC 登入成功: {provider.name}"
                + (" (auto-bound 24h API key)" if sso_api_key else "")
            ),
            commit=True,
        )
        return _build_oidc_callback_html(
            tokens,
            state_payload.get("next_path", "/"),
            api_key=sso_api_key,
        )
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


@router.post("/refresh", response_model=TokenResponse)
def refresh(request: RefreshRequest, db: Session = Depends(get_db)):
    payload = decode_token(request.refresh_token)
    user = _load_user_from_payload(payload, db, "refresh")
    return create_tokens(user)


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.put("/password")
def change_password(
    request: PasswordChangeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(request.current_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="目前密碼不正確",
        )
    current_user.hashed_password = hash_password(request.new_password)
    current_user.token_version = (current_user.token_version or 0) + 1
    db.commit()
    db.refresh(current_user)
    log_audit_event(
        db,
        actor=current_user,
        action="change_password",
        resource_type="auth",
        resource_id=current_user.id,
        detail="使用者更新自身密碼",
        commit=True,
    )
    return {"message": "密碼已更新，請重新登入", **create_tokens(current_user)}
