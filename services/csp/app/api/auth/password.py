"""Password/session endpoints: register, login, refresh, logout, me, password.

Split from the original ``app/api/auth.py`` god-module — bodies moved
verbatim; only this import header is new.
"""

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.middleware.cookies import (
    REFRESH_COOKIE_NAME,
    clear_session_cookies,
    set_session_cookies,
)
from app.models.user import User
from app.schemas.user import (
    LoginRequest,
    TokenResponse,
    PasswordChangeRequest,
    UserResponse,
    RegisterRequest,
)
from app.services.audit_service import log_audit_event
from app.services.token_revocation import commit_token_revocation
from app.services.auth_service import (
    authenticate_user,
    create_tokens,
    get_current_user,
    _load_user_from_payload,
    PENDING_APPROVAL_SENTINEL,
    LOCAL_PASSWORD_DISABLED_SENTINEL,
    TOKEN_LIFETIMES_KEY,
)
from app.utils.security import decode_token, hash_password, verify_password

from ._common import (
    _finalize_login,
    _stamp_last_login,
    _reject_when_card_only,
    router,
)


PENDING_APPROVAL_ERROR_CODE = "pending_approval"


# Back-compat alias — callers and tests historically imported this name
# from password.py; the shared implementation lives in
# ``app.services.token_revocation``.
_commit_token_revocation = commit_token_revocation


@router.post("/register", status_code=201)
def register(request: RegisterRequest, http_request: Request, db: Session = Depends(get_db)):
    _reject_when_card_only()
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
def login(
    request: LoginRequest,
    http_request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    # Branch SSO lockdown:``ANILA_AUTH_MODE=card-only`` 時帳密登入僅保留給
    # **owner**(break-glass 管理通道,2026-06-11 拍板)。posture 與
    # ``_reject_when_card_only`` 一致:非 owner 的所有結果 — 帳密錯、待核准、
    # 甚至帳密完全正確 — 一律回與「功能不存在」相同的 404,讓外部探測無法
    # 區分「密碼錯 / 權限不足 / 端點關閉」;只有 owner 完整登入成功會放行。
    card_only = settings.ANILA_AUTH_MODE == "card-only"
    ip_address = http_request.client.host if http_request.client else None

    if request.auth_source not in (None, "", "local"):
        if card_only:
            raise HTTPException(status_code=404)
        # LDAP 已自系統移除（將以 SSO 取代），僅保留本地登入 + OIDC callback。
        raise HTTPException(
            status_code=400,
            detail="僅支援本地登入；OIDC 請走 /api/auth/oidc 流程",
        )

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
        if card_only:
            raise HTTPException(status_code=404)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="帳號或密碼錯誤",
        )
    if result is PENDING_APPROVAL_SENTINEL:
        if card_only:
            raise HTTPException(status_code=404)
        # Object-shaped detail is used by this pending-login response and by
        # allow-listed upstream errors on the proxy /v1/chat/completions path;
        # consumers read detail.message and treat detail.code as metadata.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": PENDING_APPROVAL_ERROR_CODE,
                "message": "等待核准中，請通知 admin",
            },
        )
    if result is LOCAL_PASSWORD_DISABLED_SENTINEL:
        # Sprint 6 X / B2：使用者已切換到 SSO-only，引導改走 OIDC。
        log_audit_event(
            db,
            action="login",
            resource_type="auth",
            status="failure",
            detail=f"本機登入被阻擋（local_password_disabled=true）: {request.username}",
            ip_address=ip_address,
            commit=True,
        )
        if card_only:
            raise HTTPException(status_code=404)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="此帳號已切換為 SSO 登入；請改用單一登入按鈕。",
        )
    if card_only and result.role != "owner":
        # 帳密「正確」但非 owner — 這代表持有效憑證者試圖繞過卡片通道,
        # 比單純密碼錯誤更值得留痕;audit 記明原因後仍回 404 維持姿態。
        log_audit_event(
            db,
            action="login",
            resource_type="auth",
            status="failure",
            detail=f"card-only 模式下非 owner 嘗試帳密登入(憑證有效): {request.username}",
            ip_address=ip_address,
            commit=True,
        )
        raise HTTPException(status_code=404)
    tokens = create_tokens(result, db, include_lifetimes=True)
    _stamp_last_login(db, result)
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
    return _finalize_login(response, tokens, db)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    http_request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """Rotate access/refresh tokens.

    Refresh token may arrive in either:
    - The ``anila_refresh_token`` cookie (SPA, Wave 2 default; cookie is
      scoped to this path only, never leaks elsewhere), or
    - The JSON body ``{"refresh_token": "..."}`` (SDK / legacy SPA).

    On success we set fresh cookies AND return the tokens in the JSON
    body — the body keeps the SDK path working, the cookies keep the
    browser happy without JS token juggling.
    """
    token = http_request.cookies.get(REFRESH_COOKIE_NAME)
    if not token:
        try:
            payload_body = await http_request.json()
        except Exception:
            payload_body = {}
        token = (payload_body or {}).get("refresh_token")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 refresh token",
        )
    payload = decode_token(token)
    user = _load_user_from_payload(payload, db, "refresh")
    tokens = create_tokens(user, db, include_lifetimes=True)
    token_lifetimes = tokens.pop(TOKEN_LIFETIMES_KEY, None)
    set_session_cookies(
        response,
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        db=db,
        token_lifetimes=token_lifetimes,
    )
    return tokens


@router.post("/logout")
def logout(
    http_request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """Clear session cookies and bump the user's token_version.

    Bumping ``token_version`` invalidates any outstanding JWTs the user
    already issued — so logout is effective even if an attacker copied
    the access token before logout. Cookie removal handles the active
    browser tab; token_version handles everything else.
    """
    try:
        current_user = get_current_user(http_request, None, db)
    except HTTPException:
        current_user = None

    if current_user is not None:
        current_user.token_version = (current_user.token_version or 0) + 1
        _commit_token_revocation(db, current_user)
        log_audit_event(
            db,
            actor=current_user,
            action="logout",
            resource_type="auth",
            resource_id=current_user.id,
            detail="使用者登出（cookie 清除 + token_version++）",
            ip_address=http_request.client.host if http_request.client else None,
            commit=True,
        )

    clear_session_cookies(response)
    return {"message": "已登出"}


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.put("/password")
def change_password(
    request: PasswordChangeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Card-only deployments：本機帳密僅 owner 保留(break-glass,與 /login 的
    # owner 例外對齊)— owner 能登入就必須能輪換密碼。其他帳號的
    # hashed_password 是 unguessable random,本來就提供不出 current_password,
    # 對他們維持 404 姿態。
    if settings.ANILA_AUTH_MODE == "card-only" and current_user.role != "owner":
        raise HTTPException(status_code=404)
    if not verify_password(request.current_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="目前密碼不正確",
        )
    current_user.hashed_password = hash_password(request.new_password)
    current_user.token_version = (current_user.token_version or 0) + 1
    _commit_token_revocation(db, current_user)
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
    return {"message": "密碼已更新，請重新登入", **create_tokens(current_user, db)}
