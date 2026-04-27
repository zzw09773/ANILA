import hmac

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from app.config import settings
from app.database import get_db
from app.middleware.cookies import ACCESS_COOKIE_NAME
from app.models.user import User
from app.utils.security import (
    verify_password,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
)

# auto_error=False lets us fall back to the cookie when no Authorization
# header is present, instead of raising 403 immediately.
security = HTTPBearer(auto_error=False)


PENDING_APPROVAL_SENTINEL = "PENDING_APPROVAL"
LOCAL_PASSWORD_DISABLED_SENTINEL = "LOCAL_PASSWORD_DISABLED"


def authenticate_user(db: Session, username: str, password: str) -> User | str | None:
    """Validate local username/password.

    Returns:
        ``User`` on success.
        ``PENDING_APPROVAL_SENTINEL`` 若密碼正確但帳號未核准。
        ``LOCAL_PASSWORD_DISABLED_SENTINEL`` 若使用者已切到 SSO-only
            （Sprint 6 X / B2）— 本機密碼不再接受，需走 OIDC。
        ``None`` 任何其他失敗（找不到使用者 / 密碼錯 / 帳號停用）。
    """
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.hashed_password):
        return None
    if not user.is_active:
        return None
    if not getattr(user, "is_approved", True):
        return PENDING_APPROVAL_SENTINEL
    # B2: SSO-only 切換 — 即便密碼正確也拒絕，引導使用者改走 OIDC。
    if getattr(user, "local_password_disabled", False):
        return LOCAL_PASSWORD_DISABLED_SENTINEL
    return user


def create_tokens(user: User) -> dict:
    data = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "tv": user.token_version,
    }
    return {
        "access_token": create_access_token(data),
        "refresh_token": create_refresh_token(data),
        "token_type": "bearer",
    }


def _load_user_from_payload(payload: dict | None, db: Session, expected_type: str) -> User:
    if not payload or payload.get("type") != expected_type:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="無效的存取權杖" if expected_type == "access" else "無效的刷新權杖",
        )
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="無效的存取權杖",
        )
    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="使用者不存在或已停用",
        )
    if payload.get("tv", 0) != user.token_version:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="權杖已失效，請重新登入",
        )
    return user


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    """Resolve the current user from either the ``Authorization`` header
    or the ``anila_access_token`` httpOnly cookie.

    Header wins when both are present (explicit intent from SDK / curl).
    Cookie is the SPA's Wave 2 default. If neither is present, 401.
    """
    token: str | None = None
    if credentials is not None and credentials.credentials:
        token = credentials.credentials
    if token is None:
        token = request.cookies.get(ACCESS_COOKIE_NAME)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登入或權杖已過期",
        )
    payload = decode_token(token)
    return _load_user_from_payload(payload, db, "access")


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理員權限",
        )
    return current_user


def verify_service_token(
    x_csp_service_token: str | None = Header(default=None, alias="X-CSP-Service-Token"),
) -> None:
    """Auth dependency for internal service-to-service endpoints.

    Router / agents present the shared token in the X-CSP-Service-Token header.
    Uses constant-time comparison and refuses when the server has no token
    configured (fail closed).
    """
    expected = settings.CSP_SERVICE_TOKEN or ""
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="服務權杖未設定",
        )
    if not x_csp_service_token or not hmac.compare_digest(x_csp_service_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="服務權杖無效",
        )
