from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.database import get_db
from app.middleware.cookies import (
    REFRESH_COOKIE_NAME,
    clear_session_cookies,
    set_session_cookies,
)
from app.models.token_revocation import TokenRevocation
from app.models.user import User
from app.schemas.user import (
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    PasswordChangeRequest,
    UserResponse,
)
from app.services import agent_credential_service
from app.services.audit_service import log_audit_event
from app.services.auth_service import (
    authenticate_user,
    create_tokens,
    get_current_user,
    is_admin_tier,
    require_admin,
    verify_service_token,
    _load_user_from_payload,
    PENDING_APPROVAL_SENTINEL,
)
from app.services.token_revocation_publisher import publish_revocation
from app.utils.security import decode_token, hash_password, verify_password


# ── Token revocation retention ────────────────────────────────────────────
#
# The cold-start sync endpoint never returns rows older than this window.
# Matches the access-token lifetime + a safety margin: anything older
# would carry a long-expired token anyway, so replaying it is wasted
# bandwidth for anila-studio.
#
# TODO(deferred): add a daily background job that hard-deletes rows
# beyond this window. For now retention is enforced only at read time;
# the table grows linearly with revocations until manual cleanup.
TOKEN_REVOCATION_RETENTION_DAYS = 30


def _record_revocation(db: Session, *, user_id: int, version: int) -> None:
    """Insert one row into ``token_revocations`` for the cold-start
    sync endpoint to surface. Caller is expected to have already
    bumped ``users.token_version`` AND committed (or be about to in
    the same transaction). We add the row to the session but the
    caller controls the commit so the bump + insert land atomically.
    """
    db.add(
        TokenRevocation(
            user_id=user_id,
            revoked_at_version=version,
            revoked_at=datetime.now(timezone.utc),
        )
    )


class RevokeRequest(BaseModel):
    """Body for the admin-initiated revoke endpoint."""

    user_id: int


class RevokeResponse(BaseModel):
    user_id: int
    revoked_at_version: int


class RevocationEntry(BaseModel):
    user_id: int
    revoked_at_version: int
    ts: str


class RevocationListResponse(BaseModel):
    revocations: List[RevocationEntry]
    retention_days: int

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
async def logout(
    http_request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """Clear session cookies and bump the user's token_version.

    Bumping ``token_version`` invalidates any outstanding JWTs the user
    already issued — so logout is effective even if an attacker copied
    the access token before logout. Cookie removal handles the active
    browser tab; token_version handles everything else.

    The bump is durably recorded to ``token_revocations`` (so a
    cold-starting anila-studio can replay it via
    ``GET /api/auth/revocations``) and broadcast on the Redis channel
    ``anila:auth:token-revoke`` (so already-running anila-studios
    invalidate immediately). The broadcast is best-effort — if Redis
    is unreachable the DB write still wins.
    """
    try:
        current_user = get_current_user(http_request, None, db)
    except HTTPException:
        current_user = None

    if current_user is not None:
        new_version = (current_user.token_version or 0) + 1
        current_user.token_version = new_version
        _record_revocation(db, user_id=current_user.id, version=new_version)
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
        # Publish AFTER commit so subscribers never see a revocation
        # that ends up rolled back. Best-effort: the publisher
        # swallows Redis failures internally.
        await publish_revocation(
            user_id=current_user.id, revoked_at_version=new_version
        )

    clear_session_cookies(response)
    return {"message": "已登出"}


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.put("/password")
async def change_password(
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
    new_version = (current_user.token_version or 0) + 1
    current_user.token_version = new_version
    _record_revocation(db, user_id=current_user.id, version=new_version)
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
    # Best-effort broadcast — see logout for trade-off rationale.
    await publish_revocation(
        user_id=current_user.id, revoked_at_version=new_version
    )
    return {"message": "密碼已更新，請重新登入", **create_tokens(current_user)}


# ── Admin: force-revoke another user's tokens ────────────────────────────
#
# Used when ops needs to lock out an account out-of-band (compromised
# credentials, leaver, etc.). Bumps the target's ``token_version``,
# records the event, and broadcasts on the live Redis channel. The
# target's next request — header OR cookie — will fail at the ``tv``
# claim check in ``_load_user_from_payload`` and force a re-login.


@router.post("/revoke", response_model=RevokeResponse)
async def admin_revoke_user_tokens(
    request: RevokeRequest,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Force-bump ``users.token_version`` for ``request.user_id``.

    Requires admin-or-owner role. Returns 404 if the target doesn't
    exist (we don't probe-test for existence — listing users is a
    separate admin endpoint).
    """
    target = db.query(User).filter(User.id == request.user_id).first()
    if target is None:
        raise HTTPException(status_code=404, detail="使用者不存在")

    new_version = (target.token_version or 0) + 1
    target.token_version = new_version
    _record_revocation(db, user_id=target.id, version=new_version)
    db.commit()
    log_audit_event(
        db,
        actor=current_user,
        action="admin_revoke_tokens",
        resource_type="auth",
        resource_id=target.id,
        detail=(
            f"管理員 {current_user.username} 強制撤銷 user_id={target.id} "
            f"的所有 JWT（token_version → {new_version}）"
        ),
        ip_address=http_request.client.host if http_request.client else None,
        commit=True,
    )
    # Best-effort: anila-studio that's already running will react to
    # the published event; one that boots later replays via
    # GET /api/auth/revocations.
    await publish_revocation(user_id=target.id, revoked_at_version=new_version)

    return RevokeResponse(user_id=target.id, revoked_at_version=new_version)


# ── Service-to-service: cold-start sync of recent revocations ───────────
#
# anila-studio (and any future verifier) hits this on boot to seed its
# in-memory deny list before subscribing to the live Redis channel.
# The window is clamped to TOKEN_REVOCATION_RETENTION_DAYS so even a
# subscriber that's been offline for months only gets the last 30 days
# back — any older "revoked" token has expired naturally by then.


@router.get("/revocations", response_model=RevocationListResponse)
def list_revocations(
    since: datetime = Query(
        ...,
        description=(
            "Lower-bound timestamp (ISO-8601). Naive timestamps are "
            "treated as UTC. Clamped against the retention floor."
        ),
    ),
    db: Session = Depends(get_db),
    _identity: agent_credential_service.CallerIdentity | None = Depends(
        verify_service_token
    ),
):
    """Return revocation events after ``since``, capped to the
    retention window.

    Auth: ``X-CSP-Service-Token`` header. Both the DB-backed token
    paths AND the legacy env-var fallback are accepted (anila-studio
    in fresh deployments has no DB row yet). Admin JWTs are NOT
    accepted — single auth path simplifies the client.

    Sorted ASC by ``revoked_at`` so subscribers can apply events in
    monotonic order without sorting client-side.
    """
    # ``since`` may arrive naive (no tzinfo) — treat as UTC so the
    # comparison against ``revoked_at`` (which is timezone-aware)
    # doesn't blow up at SQLAlchemy level on Postgres.
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    retention_floor = datetime.now(timezone.utc) - timedelta(
        days=TOKEN_REVOCATION_RETENTION_DAYS
    )
    effective_since = max(since, retention_floor)

    rows = (
        db.query(TokenRevocation)
        .filter(TokenRevocation.revoked_at >= effective_since)
        .order_by(TokenRevocation.revoked_at.asc())
        .all()
    )

    return RevocationListResponse(
        revocations=[
            RevocationEntry(
                user_id=row.user_id,
                revoked_at_version=row.revoked_at_version,
                ts=_serialise_ts(row.revoked_at),
            )
            for row in rows
        ],
        retention_days=TOKEN_REVOCATION_RETENTION_DAYS,
    )


def _serialise_ts(ts: datetime) -> str:
    """Render a UTC ISO-8601 string with a ``Z`` suffix.

    SQLite returns timezone-naive datetimes for ``DateTime(timezone=
    True)`` columns — assume UTC. Postgres returns tz-aware; honour
    whatever tz the DB sends back.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
