import hmac
import logging

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from app.config import settings
from app.database import get_db
from app.middleware.cookies import ACCESS_COOKIE_NAME
from app.models.user import User
from app.services import agent_credential_service
from app.services.audit_service import log_audit_event
from app.utils.security import (
    verify_password,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    resolve_token_lifetimes,
)

logger = logging.getLogger(__name__)

# auto_error=False lets us fall back to the cookie when no Authorization
# header is present, instead of raising 403 immediately.
security = HTTPBearer(auto_error=False)


PENDING_APPROVAL_SENTINEL = "PENDING_APPROVAL"
LOCAL_PASSWORD_DISABLED_SENTINEL = "LOCAL_PASSWORD_DISABLED"
TOKEN_LIFETIMES_KEY = "_token_lifetimes"


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


def create_tokens(
    user: User,
    db: Session | None = None,
    *,
    include_lifetimes: bool = False,
) -> dict:
    data = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "tv": user.token_version,
    }
    access_minutes, refresh_days = resolve_token_lifetimes(db)
    tokens = {
        "access_token": create_access_token(
            data, db=db, lifetime_minutes=access_minutes
        ),
        "refresh_token": create_refresh_token(
            data, db=db, lifetime_days=refresh_days
        ),
        "token_type": "bearer",
    }
    if include_lifetimes:
        # Internal metadata is consumed before any response model serialises
        # the token dict; it keeps cookie Max-Age tied to these exact values.
        tokens[TOKEN_LIFETIMES_KEY] = (access_minutes, refresh_days)
    return tokens


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


_ADMIN_TIER_ROLES = ("admin", "owner")


def is_owner(user: User) -> bool:
    """Single check used by serializers / response shaping that needs to
    decide whether to surface owner-only fields (e.g. model endpoint URL,
    audit log IP / metadata)."""
    return user.role == "owner"


def is_admin_tier(user: User) -> bool:
    """True for both `admin` and the higher `owner` role.

    Use this everywhere old code does ``user.role == "admin"`` to gate
    a "sees everything / can edit anything" data scope. Without this,
    owner accounts get treated as regular users by per-owner_user_id /
    per-grant filters and the admin UI silently hides rows the owner
    didn't personally create — see the 2026-05-11 regression where
    CSP /api/agents listed only owner-registered agents while ANILA UI
    still surfaced others, making operators think a delete had partially
    succeeded.
    """
    return user.role in _ADMIN_TIER_ROLES


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """Tier check: ``admin`` and the higher ``owner`` both satisfy.

    Owner inherits all admin privileges by design — the ``owner`` tier
    sits ABOVE admin, so any endpoint that accepts admins must also
    accept owners. Endpoints that owner-only should depend on
    :func:`require_owner` directly instead.
    """
    if current_user.role not in _ADMIN_TIER_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理員權限",
        )
    return current_user


def require_owner(current_user: User = Depends(get_current_user)) -> User:
    """Top-tier gate. Reserved for irreversible / platform-altering ops:
    promoting/demoting admins, rotating SECRET_KEY, editing auth
    providers, hard-purging registry rows, viewing raw audit log fields.
    """
    if current_user.role != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要 owner 權限",
        )
    return current_user


def verify_service_token(
    request: Request,
    db: Session = Depends(get_db),
    x_csp_service_token: str | None = Header(default=None, alias="X-CSP-Service-Token"),
) -> "agent_credential_service.CallerIdentity | None":
    """Auth dependency for internal service-to-service endpoints.

    Sprint 8 X / Phase A — DB-backed verify. Resolves the token in this
    order:

      1. ``service_clients`` (Router / worker / platform s2s). On a stock
         post-migration-0027 deploy the host ``CSP_SERVICE_TOKEN`` is
         seeded as ``client_name='router-primary'``, so the fleet secret
         matches **here** — attributed ``service_client``, not step 3.
         Long-lived ``agent_credentials`` are not consulted. Agents use
         the dispatch JWT; migration 0027 rows must not become a caller
         when ``CSP_SERVICE_TOKEN`` is unset.
      2. ``settings.CSP_SERVICE_TOKEN`` env-var fallback — only reached
         when no active DB row matches. Hits write
         ``service_token_legacy_env_used`` (Signal A / legacy-token-stats).
         That signal is often already **zero** while step 1 still serves
         the shared secret via the seeded ``router-primary`` row.

    Do **not** delete this env branch because Signal A reads zero. The
    gate for removing step 3 is Signal B = 0 for a full release window:
    ``COUNT(*)`` of active ``is_legacy=TRUE`` rows in both
    ``service_clients`` and ``agent_credentials`` (see
    ``docs/runbooks/service-token-cutover.md`` Stage 3/4). On a stock
    deploy Signal B is **non-zero today** (the ``router-primary`` row).

    On match we attach the ``CallerIdentity`` to ``request.state`` so
    downstream handlers / proxy / usage_writer can read who triggered
    the call without re-doing the lookup. Returns the identity (or
    ``None`` for legacy env-var path so callers can still distinguish).
    """
    if not x_csp_service_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 X-CSP-Service-Token header",
        )

    # 每服務憑證檔啟用後，舊的共用祕密不是任何服務身分。
    # 在資料庫查找與環境變數後援之前拒絕，避免它仍被當成 router-primary。
    if agent_credential_service.fleet_secret_retired(x_csp_service_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="服務權杖無效",
        )

    # service_clients。長效 agent_credentials 不再是身分。
    identity = agent_credential_service.verify_service_token(
        db, token=x_csp_service_token
    )
    if identity is not None:
        request.state.csp_caller = identity
        return identity

    # 3) Legacy env-var fallback. Removed in cutover step 5 (see
    #    docs/runbooks/service-token-cutover.md).
    legacy = (settings.CSP_SERVICE_TOKEN or "").strip()
    if legacy and hmac.compare_digest(x_csp_service_token, legacy):
        request.state.csp_caller = None  # explicit: legacy = unattributed
        try:
            log_audit_event(
                db,
                actor=None,
                action=agent_credential_service.AUDIT_LEGACY_TOKEN_USED,
                resource_type="service_token",
                resource_id=None,
                detail=(
                    "CSP_SERVICE_TOKEN env-var fallback hit — caller "
                    "unattributed. Schedule per-agent cutover."
                ),
                ip_address=getattr(request.client, "host", None),
                commit=True,
            )
        except Exception:  # noqa: BLE001 — never let audit fail the request
            logger.exception("Failed to write legacy_service_token_used audit event")
        return None

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="服務權杖無效",
    )
