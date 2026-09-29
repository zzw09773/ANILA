import logging
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import text, update
from sqlalchemy.orm import Session
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


class InactivityDisabled:
    """密碼正確，但帳號是閒置停用。呼叫端在確認這條登入路徑允許之後才改回待審。"""

    def __init__(self, user: User):
        self.user = user


def record_successful_login(db: Session, user: User) -> bool:
    """成功登入的時間戳。只在帳號仍啟用且已核准時寫入。

    必須在發 token 之前呼叫。影響 0 列代表停用或取消核准搶先提交，
    呼叫端不得發 token。
    """
    now = datetime.now(timezone.utc)
    result = db.execute(
        update(User)
        .where(User.id == user.id)
        .where(User.is_active.is_(True))
        .where(User.is_approved.is_(True))
        .values(last_login_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        return False
    db.commit()
    user.last_login_at = now
    return True


def authenticate_user(db: Session, username: str, password: str) -> User | str | None:
    """Validate local username/password.

    Returns:
        ``User`` on success. 不在這裡寫 last_login_at；呼叫端要等
        卡片限定之類的放行檢查通過，發 token 之前才寫。
        ``PENDING_APPROVAL_SENTINEL`` 若密碼正確但帳號未核准。
        ``LOCAL_PASSWORD_DISABLED_SENTINEL`` 若使用者已切到 SSO-only
            （Sprint 6 X / B2）— 本機密碼不再接受，需走 OIDC。
        ``InactivityDisabled`` 密碼正確且是閒置停用。還沒改狀態。
        ``None`` 任何其他失敗（找不到使用者 / 密碼錯 / 其他原因停用）。
    """
    from app.services.inactivity_service import DISABLED_REASON_INACTIVITY

    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.hashed_password):
        return None
    # 帳密這條不允許時，不要把閒置停用改回待審，也不要動錨點。
    if getattr(user, "local_password_disabled", False):
        return LOCAL_PASSWORD_DISABLED_SENTINEL
    if not user.is_active:
        if user.disabled_reason == DISABLED_REASON_INACTIVITY:
            return InactivityDisabled(user)
        return None
    if not getattr(user, "is_approved", True):
        return PENDING_APPROVAL_SENTINEL
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
    return resolve_presented_user(token, db)


def resolve_presented_user(token: str, db: Session) -> User:
    """Access token，或 Studio 工作委託權杖。後者失敗時不退回存取權杖解碼。"""
    from app.services.studio_job_token import (
        load_studio_job_user,
        presented_studio_job_token,
        verify_studio_job_token,
    )

    if presented_studio_job_token(token):
        claims = verify_studio_job_token(token, db)
        if claims is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="工作委託權杖無效或已過期",
            )
        return load_studio_job_user(claims, db)
    payload = decode_token(token, db=db)
    return _load_user_from_payload(payload, db, "access")


def require_interactive_user(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> User:
    """密碼、API Key、重發工作權杖只接受使用者自己的工作階段。"""
    from app.services.studio_job_token import reject_studio_job_credential

    reject_studio_job_credential(request)
    return current_user


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


DEPUTY_ROLE = "deputy"
DEPUTY_CAP = 3
# Transaction advisory lock. Fixed key so create / promote / approve /
# re-enable serialize on the same count. Not a secret.
DEPUTY_CAP_ADVISORY_LOCK_KEY = 0x44505554


def lock_deputy_capacity(db: Session) -> None:
    """Hold the deputy-cap lock until this session commits or rolls back.

    PostgreSQL uses a transaction advisory lock. SQLite has no advisory
    lock and drops ``FOR UPDATE``, so tests take ``BEGIN IMMEDIATE`` and
    keep the write lock until commit. SQLite 這條會先 rollback；呼叫端
    不能有尚未提交的變更。
    """
    if db.info.get("deputy_cap_locked"):
        return
    bind = db.get_bind()
    dialect = bind.dialect.name if bind is not None else ""
    if dialect == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:k)"),
            {"k": DEPUTY_CAP_ADVISORY_LOCK_KEY},
        )
    elif dialect == "sqlite":
        db.rollback()
        raw = db.connection().connection.driver_connection
        raw.execute("BEGIN IMMEDIATE")
    else:
        return
    db.info["deputy_cap_locked"] = True


def is_deputy(user: User) -> bool:
    """代理管理員。不是 admin tier，不能靠 is_admin_tier 繼承管理員權限。"""
    return user.role == DEPUTY_ROLE


def is_steward(user: User) -> bool:
    """核准、警報、公告、回饋、儀表板這一層。admin 與 owner 都算。"""
    return is_admin_tier(user) or is_deputy(user)


def require_steward(current_user: User = Depends(get_current_user)) -> User:
    if not is_steward(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理員權限",
        )
    return current_user


def active_deputy_count(
    db: Session,
    *,
    exclude_user_id: int | None = None,
    exclude_user_ids: list[int] | None = None,
) -> int:
    """佔用代理名額的人數。

    規則：角色是代理管理員就佔一個名額，不論是否已核准、是否啟用。
    停用不會空出名額；要讓出名額，得先把角色改掉。
    建立、把角色改成代理、核准、批次核准、重新啟用都用這個人數。
    已經是代理的人，核准或重新啟用不會再佔一次（呼叫端排除本人）。
    """
    query = db.query(User).filter(User.role == DEPUTY_ROLE)
    if exclude_user_id is not None:
        query = query.filter(User.id != exclude_user_id)
    if exclude_user_ids:
        query = query.filter(User.id.notin_(list(exclude_user_ids)))
    return query.count()


def ensure_deputy_capacity(db: Session, *, exclude_user_id: int | None = None) -> None:
    lock_deputy_capacity(db)
    if active_deputy_count(db, exclude_user_id=exclude_user_id) >= DEPUTY_CAP:
        raise HTTPException(status_code=409, detail="代理管理員最多 3 人")


def guard_deputy_role_change(
    db: Session,
    *,
    actor: User,
    current_role: str | None,
    new_role: str | None,
    will_be_counted: bool,
) -> None:
    """只有擁有者可以指派或拿掉代理管理員。不能把擁有者改成代理。

    新的代理不得超過 3 人。名額在角色變成代理的當下就佔用，
    不論該帳號是否已核准或啟用。見 ``active_deputy_count``。
    """
    if new_role == DEPUTY_ROLE and current_role == "owner":
        raise HTTPException(status_code=403, detail="不能把擁有者改成代理管理員")
    assigning = new_role == DEPUTY_ROLE and current_role != DEPUTY_ROLE
    removing = (
        current_role == DEPUTY_ROLE
        and new_role is not None
        and new_role != DEPUTY_ROLE
    )
    if (assigning or removing) and not is_owner(actor):
        raise HTTPException(status_code=403, detail="只有擁有者可以指派代理管理員")
    if assigning and will_be_counted:
        ensure_deputy_capacity(db)


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

      1. ``service_clients`` (Router / worker / platform s2s).
         Long-lived ``agent_credentials`` are not consulted. Agents use
         the dispatch JWT.
      2. 沒有對上的資料列就 401。舊的共用 ``CSP_SERVICE_TOKEN`` 不再放行。
         沒有對上的權杖一律 401。``is_legacy`` 列不是身分。

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

    # service_clients。is_legacy 列與長效 agent_credentials 都不是身分。
    identity = agent_credential_service.verify_service_token(
        db, token=x_csp_service_token
    )
    if identity is not None:
        request.state.csp_caller = identity
        return identity

    # 舊的共用 CSP_SERVICE_TOKEN 不再是呼叫者。對得上那把祕密時，
    # 沒有對上的權杖一律 401。
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="服務權杖無效",
    )
