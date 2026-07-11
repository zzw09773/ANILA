import hmac
import logging
from collections.abc import Sequence

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
)

logger = logging.getLogger(__name__)

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


def create_tokens(user: User, *, amr: Sequence[str] = ()) -> dict:
    """Create a token pair with an explicit authentication-method claim.

    Legacy/internal callers that omit ``amr`` receive an empty claim and
    therefore cannot be mistaken for a smart-card session by proxy gates.
    """
    data = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "tv": user.token_version,
        "amr": list(amr),
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
    if settings.REQUIRE_CARD_LOGIN_ONLY:
        raw_amr = payload.get("amr")
        methods = (
            set(raw_amr)
            if isinstance(raw_amr, list)
            and all(isinstance(method, str) for method in raw_amr)
            else set()
        )
        # Formal intranet sessions must prove that the current token descends
        # from a smart-card login.  The sole break-glass exception is a
        # password-authenticated *current DB owner*.  Deliberately do not trust
        # the role copied into the JWT: an old token must not retain owner
        # assurance after the account has been demoted in the database.
        card_assured = "sc" in methods
        owner_break_glass = user.role == "owner" and "pwd" in methods
        if not (card_assured or owner_break_glass):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="此部署僅接受憑證卡工作階段，請重新登入",
            )
    return user


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    """Resolve the current user from either the ``Authorization`` header
    or the configured ``__Host-anila_access_token`` httpOnly cookie
    (``anila_dev_access_token`` only in explicit insecure test/dev mode).

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
    user = _load_user_from_payload(payload, db, "access")
    # Make verified claims available to narrowly scoped authorization probes.
    # This assignment occurs only after signature/type/user/version checks.
    request.state.auth_claims = payload
    return user


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

      1. ``service_clients`` (Router / worker traffic).
      2. ``agent_credentials`` (per-agent traffic).
      3. ``settings.CSP_SERVICE_TOKEN`` env var (legacy fleet-shared
         fallback). Hits also write a ``service_token_legacy_env_used``
         audit event so admins can watch cutover progress in the
         dashboard. The fallback is removed entirely once the cutover
         dashboard widget shows zero hits for a release window.

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

    # 1) + 2) DB lookup.
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
