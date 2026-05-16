from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.middleware.cookies import (
    REFRESH_COOKIE_NAME,
    clear_session_cookies,
    set_session_cookies,
)
from app.models.user import User
from app.schemas.user import (
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    PasswordChangeRequest,
    UserResponse,
)
from app.services.audit_service import log_audit_event
from app.services.auth_service import (
    authenticate_user,
    create_tokens,
    get_current_user,
    _load_user_from_payload,
    PENDING_APPROVAL_SENTINEL,
)
from app.utils.security import decode_token, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["認證"])


def _finalize_login(response: Response, tokens: dict) -> dict:
    """Attach session cookies to the response and surface the CSRF token
    in the JSON body so the SPA can read it even on its first request."""
    csrf = set_session_cookies(
        response,
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
    )
    return {**tokens, "csrf_token": csrf}


def _stamp_last_login(db: Session, user: User) -> None:
    """Record the current timestamp on the user's profile so the admin
    user panel can show ``last_login_at`` without scanning the audit log."""
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()


@router.post("/register", status_code=201)
def register(
    request: RegisterRequest,
    http_request: Request,
    db: Session = Depends(get_db),
):
    """Self-service signup. Account starts with ``is_approved=False`` —
    user must wait for an admin to approve before login succeeds.

    Closed-deployment safety:
    - Password validated by ``RegisterRequest.password_strength``
      (8+ chars / mixed case / symbol) — same policy as admin-set passwords
    - Username collision returns 400 verbatim so the SPA can show
      "帳號已被使用" without disclosing whether the email is also taken
    - audit_log records both successful registers and collisions, so
      probes for existing usernames leave a trail
    """
    ip_address = http_request.client.host if http_request.client else None

    existing = db.query(User).filter(User.username == request.username).first()
    if existing:
        log_audit_event(
            db,
            action="register",
            resource_type="auth",
            status="failure",
            detail=f"註冊衝突: {request.username}",
            ip_address=ip_address,
            commit=True,
        )
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
    db.refresh(user)
    log_audit_event(
        db,
        actor=user,
        action="register",
        resource_type="auth",
        resource_id=user.id,
        detail="使用者送出註冊申請（待 admin 核准）",
        ip_address=ip_address,
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
    ip_address = http_request.client.host if http_request.client else None

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
            detail="帳號尚未開通…請聯絡管理員",
        )
    tokens = create_tokens(result)
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
    return _finalize_login(response, tokens)


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
    tokens = create_tokens(user)
    set_session_cookies(
        response,
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
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
        db.commit()
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
