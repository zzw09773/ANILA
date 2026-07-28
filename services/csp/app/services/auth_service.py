import hmac
import json
import logging
import secrets
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session, defer
from app.config import settings
from app.database import get_db
from app.middleware.cookies import ACCESS_COOKIE_NAME
from app.models.audit_log import AuditLog
from app.models.auth_session import AuthRefreshToken, AuthSession
from app.models.user import User
from app.services import agent_credential_service
from app.services.audit_service import log_audit_event
from app.services.startup_security import (
    break_glass_audit_metadata,
    is_break_glass_active,
)
from app.services.token_revocation_service import (
    is_revoked as is_identifier_revoked,
    revoke_sid as persist_sid_revocation,
)
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


_ACR_BY_AMR = {
    "pwd": "urn:anila:acr:password",
    "oidc": "urn:anila:acr:federated",
    "sc": "urn:anila:acr:smart-card",
}


class RefreshTokenReuseDetected(Exception):
    """A consumed refresh-token generation was presented again."""

    def __init__(self, *, token_jti_hash: str, session_id_hash: str) -> None:
        super().__init__("refresh token reuse detected")
        self.token_jti_hash = token_jti_hash
        self.session_id_hash = session_id_hash


def _identifier_hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _normalise_amr(amr: Sequence[str]) -> tuple[str, ...]:
    methods = tuple(dict.fromkeys(method for method in amr if method))
    if any(method not in _ACR_BY_AMR for method in methods):
        raise ValueError("unsupported authentication method reference")
    return methods


def _acr_for(methods: Sequence[str], *, break_glass: bool) -> str:
    if break_glass:
        return "urn:anila:acr:break-glass"
    for method in ("sc", "oidc", "pwd"):
        if method in methods:
            return _ACR_BY_AMR[method]
    return "urn:anila:acr:unspecified"


def _epoch_to_datetime(value: int | float) -> datetime:
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _active_break_glass_binding() -> tuple[str, datetime] | None:
    metadata = break_glass_audit_metadata()
    if metadata is None:
        return None
    ticket = metadata.get("ticket")
    expires_raw = metadata.get("expires_at")
    if not isinstance(ticket, str) or not ticket:
        return None
    try:
        expires_at = datetime.fromisoformat(str(expires_raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if expires_at.tzinfo is None:
        return None
    return ticket, expires_at.astimezone(timezone.utc)


def _break_glass_expiry_claim(payload: dict) -> datetime | None:
    raw = payload.get("break_glass_expires_at")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("invalid break-glass expiry claim")
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("break-glass expiry claim requires timezone")
    return value.astimezone(timezone.utc)


def _sessionless_tokens_allowed() -> bool:
    return settings.ANILA_DEPLOYMENT_PROFILE.strip().lower() in {
        "development",
        "dev",
        "test",
    }


def create_tokens(
    user: User,
    *,
    db: Session | None = None,
    amr: Sequence[str] = (),
    sid: str | None = None,
    auth_time: int | None = None,
    acr: str | None = None,
    break_glass: bool | None = None,
    break_glass_ticket: str | None = None,
    break_glass_expires_at: datetime | None = None,
    refresh_generation: int = 0,
    parent_refresh_jti_hash: str | None = None,
) -> dict:
    """Create a token pair with an explicit authentication-method claim.

    Legacy/internal callers that omit ``amr`` receive an empty claim and
    therefore cannot be mistaken for a smart-card session by proxy gates.
    """
    methods = _normalise_amr(amr)
    now = datetime.now(timezone.utc)
    issued_at = int(now.timestamp())
    session_id = sid or secrets.token_urlsafe(32)
    access_jti = secrets.token_urlsafe(32)
    refresh_jti = secrets.token_urlsafe(32)
    existing_session = db.get(AuthSession, session_id) if db is not None else None
    if break_glass is None:
        break_glass = (
            "pwd" in methods
            and settings.REQUIRE_CARD_LOGIN_ONLY
            and is_break_glass_active()
        )
    if break_glass and methods != ("pwd",):
        raise ValueError("break-glass assurance requires password-only AMR")
    if break_glass:
        active_binding = _active_break_glass_binding()
        if break_glass_ticket is None or break_glass_expires_at is None:
            if active_binding is not None:
                break_glass_ticket, break_glass_expires_at = active_binding
        if not break_glass_ticket or break_glass_expires_at is None:
            if existing_session is not None:
                raise ValueError("cannot change assurance for an existing auth session")
            raise ValueError("break-glass assurance requires an active incident binding")
        break_glass_expires_at = _as_utc(break_glass_expires_at)
        if active_binding != (break_glass_ticket, break_glass_expires_at):
            raise ValueError("break-glass assurance does not match the active incident")
    elif break_glass_ticket is not None or break_glass_expires_at is not None:
        raise ValueError("ordinary assurance cannot carry break-glass incident metadata")
    expected_assurance = _acr_for(methods, break_glass=bool(break_glass))
    if acr is not None and acr != expected_assurance:
        raise ValueError("ACR contradicts the authentication method reference")
    assurance = expected_assurance
    authenticated_at = auth_time if auth_time is not None else issued_at
    if (
        isinstance(authenticated_at, bool)
        or not isinstance(authenticated_at, (int, float))
        or authenticated_at < 0
        or authenticated_at > issued_at + settings.JWT_LEEWAY_SECONDS
    ):
        raise ValueError("auth_time is outside the token issuance boundary")
    authenticated_at = int(authenticated_at)

    session: AuthSession | None = None
    if db is not None:
        session = existing_session
        if session is not None:
            if session.user_id != user.id or session.revoked_at is not None:
                raise ValueError("cannot issue tokens for an invalid auth session")
            durable_assurance = _assurance_from_session(session)
            requested_assurance = (
                methods,
                assurance,
                authenticated_at,
                bool(break_glass),
                break_glass_ticket,
                break_glass_expires_at,
            )
            if durable_assurance != requested_assurance:
                raise ValueError("cannot change assurance for an existing auth session")

    common = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "tv": user.token_version,
        "sid": session_id,
        "iat": issued_at,
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "amr": list(methods),
        "acr": assurance,
        "auth_time": authenticated_at,
        "break_glass": bool(break_glass),
        "break_glass_ticket": break_glass_ticket,
        "break_glass_expires_at": (
            break_glass_expires_at.isoformat()
            if break_glass_expires_at is not None
            else None
        ),
    }
    access_data = {**common, "jti": access_jti}
    refresh_data = {**common, "jti": refresh_jti}
    pair = {
        "access_token": create_access_token(access_data),
        "refresh_token": create_refresh_token(refresh_data),
        "token_type": "bearer",
    }

    if db is not None:
        if session is None:
            session = AuthSession(
                sid=session_id,
                user_id=user.id,
                refresh_family_id=secrets.token_urlsafe(32),
                amr_json=json.dumps(list(methods), separators=(",", ":")),
                acr=assurance,
                auth_time=_epoch_to_datetime(authenticated_at),
                break_glass=bool(break_glass),
                break_glass_ticket=break_glass_ticket,
                break_glass_expires_at=break_glass_expires_at,
            )
            db.add(session)
            # Flush the parent explicitly. Without an ORM relationship the
            # unit-of-work does not reliably order these two mapper inserts on
            # PostgreSQL, and the refresh FK can race ahead of auth_sessions.
            db.flush()
        db.add(
            AuthRefreshToken(
                jti_hash=_identifier_hash(refresh_jti),
                sid=session_id,
                generation=refresh_generation,
                parent_jti_hash=parent_refresh_jti_hash,
                issued_at=now,
                expires_at=now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
            )
        )
        db.flush()

    return pair


def _assurance_from_session(
    session: AuthSession,
) -> tuple[tuple[str, ...], str, int, bool, str | None, datetime | None]:
    """Load the durable assurance envelope without repairing corrupt state.

    A session id is the security boundary for a refresh family.  Treating a
    malformed row as an empty/default assurance would let the same ``sid`` be
    reissued at a different assurance level, so every field is validated and
    compared exactly at issuance and request boundaries.
    """
    try:
        raw = json.loads(session.amr_json)
    except (TypeError, ValueError) as exc:
        raise ValueError("auth session contains invalid AMR state") from exc
    if (
        not isinstance(raw, list)
        or any(not isinstance(item, str) or not item for item in raw)
        or len(raw) != len(set(raw))
    ):
        raise ValueError("auth session contains invalid AMR state")
    methods = _normalise_amr(raw)

    if not isinstance(session.break_glass, bool):
        raise ValueError("auth session contains invalid break-glass state")
    expected_acr = _acr_for(methods, break_glass=session.break_glass)
    if not isinstance(session.acr, str) or session.acr != expected_acr:
        raise ValueError("auth session contains contradictory assurance state")
    if session.break_glass and methods != ("pwd",):
        raise ValueError("auth session contains contradictory break-glass state")
    ticket = session.break_glass_ticket
    expires_at = session.break_glass_expires_at
    if session.break_glass:
        if not isinstance(ticket, str) or not ticket or not isinstance(expires_at, datetime):
            raise ValueError("auth session lacks break-glass incident binding")
        expires_at = _as_utc(expires_at)
    elif ticket is not None or expires_at is not None:
        raise ValueError("ordinary auth session contains break-glass incident binding")

    auth_time = session.auth_time
    if not isinstance(auth_time, datetime):
        raise ValueError("auth session contains invalid authentication time")
    if auth_time.tzinfo is None:
        auth_time = auth_time.replace(tzinfo=timezone.utc)
    authenticated_at = int(auth_time.timestamp())
    if (
        authenticated_at < 0
        or authenticated_at
        > int(datetime.now(timezone.utc).timestamp()) + settings.JWT_LEEWAY_SECONDS
    ):
        raise ValueError("auth session contains invalid authentication time")
    return (
        methods,
        session.acr,
        authenticated_at,
        session.break_glass,
        ticket,
        expires_at,
    )


def _revoke_auth_session(db: Session, session: AuthSession, *, reason: str) -> None:
    now = datetime.now(timezone.utc)
    session.revoked_at = now
    session.revoke_reason = reason
    (
        db.query(AuthRefreshToken)
        .filter(
            AuthRefreshToken.sid == session.sid,
            AuthRefreshToken.revoked_at.is_(None),
        )
        .update({"revoked_at": now}, synchronize_session=False)
    )


def rotate_refresh_token(
    db: Session,
    user: User,
    payload: dict,
    *,
    ip_address: str | None = None,
) -> dict:
    """Atomically consume one refresh generation and mint its successor."""
    raw_jti = payload.get("jti")
    sid = payload.get("sid")
    if not isinstance(raw_jti, str) or not isinstance(sid, str):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="無效的刷新權杖",
        )
    jti_hash = _identifier_hash(raw_jti)
    session = (
        db.query(AuthSession)
        .filter(AuthSession.sid == sid, AuthSession.user_id == user.id)
        .with_for_update().populate_existing()
        .first()
    )
    record = (
        db.query(AuthRefreshToken)
        .filter(AuthRefreshToken.jti_hash == jti_hash)
        .with_for_update().populate_existing()
        .first()
    )

    # Compatibility for session tokens minted directly by test/dev helpers
    # before persistence was introduced. Formal profiles reject missing state.
    if session is None or record is None:
        if not _sessionless_tokens_allowed():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="刷新工作階段不存在",
            )
        if session is None:
            raw_amr = payload.get("amr")
            methods = (
                tuple(raw_amr)
                if isinstance(raw_amr, list)
                and all(isinstance(method, str) for method in raw_amr)
                else ()
            )
            auth_time = int(payload.get("auth_time") or payload["iat"])
            session = AuthSession(
                sid=sid,
                user_id=user.id,
                refresh_family_id=secrets.token_urlsafe(32),
                amr_json=json.dumps(list(methods), separators=(",", ":")),
                acr=str(payload.get("acr") or _acr_for(methods, break_glass=False)),
                auth_time=_epoch_to_datetime(auth_time),
                break_glass=bool(payload.get("break_glass", False)),
                break_glass_ticket=payload.get("break_glass_ticket"),
                break_glass_expires_at=_break_glass_expiry_claim(payload),
            )
            db.add(session)
            db.flush()
        if record is None:
            record = AuthRefreshToken(
                jti_hash=jti_hash,
                sid=sid,
                generation=0,
                issued_at=_epoch_to_datetime(int(payload["iat"])),
                expires_at=_epoch_to_datetime(int(payload["exp"])),
            )
            db.add(record)
            db.flush()

    if session.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="刷新工作階段已撤銷",
        )

    # Cross-sid mismatch always takes the strict reuse path, even inside the
    # grace window. Same-sid consumed tokens may be graced briefly so concurrent
    # multi-tab refreshes do not revoke the whole family.
    if record.sid != sid or record.revoked_at is not None:
        _reject_refresh_reuse(
            db,
            user=user,
            sid=sid,
            jti_hash=jti_hash,
            ip_address=ip_address,
        )

    if record.consumed_at is not None:
        # 寬限路徑：正式姿態由 startup_security 強制 ANILA_REFRESH_REUSE_GRACE_SECONDS=0
        # 關閉。啟用時並非 replay-bounded——consumed token 在窗內重播會重選 tip
        # 再簽一對，且剛消耗的 tip 會串成新窗（daisy-chain）。追蹤中改為
        # idempotent-recovery 再重設；此處僅維持現有演算法 + 時鐘偏移防呆。
        now = datetime.now(timezone.utc)
        consumed_at = record.consumed_at
        if consumed_at.tzinfo is None:
            consumed_at = consumed_at.replace(tzinfo=timezone.utc)
        age_seconds = (now - consumed_at).total_seconds()
        within_grace = (
            session.revoked_at is None
            and age_seconds >= 0
            and age_seconds <= settings.ANILA_REFRESH_REUSE_GRACE_SECONDS
        )
        if within_grace:
            tip = (
                db.query(AuthRefreshToken)
                .filter(
                    AuthRefreshToken.sid == sid,
                    AuthRefreshToken.consumed_at.is_(None),
                    AuthRefreshToken.revoked_at.is_(None),
                )
                .order_by(AuthRefreshToken.generation.desc())
                .with_for_update().populate_existing()
                .first()
            )
            if tip is not None:
                tip.consumed_at = now
                (
                    methods,
                    assurance,
                    authenticated_at,
                    break_glass,
                    break_glass_ticket,
                    break_glass_expires_at,
                ) = _assurance_from_session(session)
                pair = create_tokens(
                    user,
                    db=db,
                    amr=methods,
                    sid=session.sid,
                    auth_time=authenticated_at,
                    acr=assurance,
                    break_glass=break_glass,
                    break_glass_ticket=break_glass_ticket,
                    break_glass_expires_at=break_glass_expires_at,
                    refresh_generation=tip.generation + 1,
                    parent_refresh_jti_hash=tip.jti_hash,
                )
                sid_hash = _identifier_hash(sid)
                db.add(
                    AuditLog(
                        actor_user_id=user.id,
                        actor_username=user.username,
                        action="auth.refresh_reuse_graced",
                        resource_type="auth_session",
                        resource_id=sid_hash,
                        status="warning",
                        detail=(
                            "已使用的 refresh token 於寬限窗內再次出現；"
                            "延續目前工作階段"
                        ),
                        ip_address=ip_address,
                        metadata_json=json.dumps(
                            {
                                "reason": "refresh_token_reuse",
                                "token_jti_hash": jti_hash,
                                "session_id_hash": sid_hash,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    )
                )
                db.commit()
                return pair
        _reject_refresh_reuse(
            db,
            user=user,
            sid=sid,
            jti_hash=jti_hash,
            ip_address=ip_address,
        )

    record.consumed_at = datetime.now(timezone.utc)
    (
        methods,
        assurance,
        authenticated_at,
        break_glass,
        break_glass_ticket,
        break_glass_expires_at,
    ) = _assurance_from_session(session)
    pair = create_tokens(
        user,
        db=db,
        amr=methods,
        sid=session.sid,
        auth_time=authenticated_at,
        acr=assurance,
        break_glass=break_glass,
        break_glass_ticket=break_glass_ticket,
        break_glass_expires_at=break_glass_expires_at,
        refresh_generation=record.generation + 1,
        parent_refresh_jti_hash=record.jti_hash,
    )
    db.commit()
    return pair


def _reject_refresh_reuse(
    db: Session,
    *,
    user: User,
    sid: str,
    jti_hash: str,
    ip_address: str | None,
) -> None:
    sid_hash = _identifier_hash(sid)
    persist_sid_revocation(
        db,
        user_id=user.id,
        sid=sid,
        reason="refresh_token_reuse",
        commit=False,
    )
    db.add(
        AuditLog(
            actor_user_id=user.id,
            actor_username=user.username,
            action="auth.refresh_reuse",
            resource_type="auth_session",
            resource_id=sid_hash,
            status="failure",
            detail=(
                "已使用的 refresh token 再次出現；撤銷目前工作階段"
            ),
            ip_address=ip_address,
            metadata_json=json.dumps(
                {
                    "reason": "refresh_token_reuse",
                    "token_jti_hash": jti_hash,
                    "session_id_hash": sid_hash,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
    )
    try:
        # Revocation and the theft-signal audit are one durable unit. A
        # failure must not return a normal 401 while leaving the family
        # active and the incident unrecorded.
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("failed to persist refresh-token reuse incident")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="無法安全記錄刷新權杖重放事件",
        )
    raise RefreshTokenReuseDetected(
        token_jti_hash=jti_hash,
        session_id_hash=sid_hash,
    )


def _load_user_from_payload(payload: dict | None, db: Session, expected_type: str) -> User:
    if not payload or payload.get("type") != expected_type:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="無效的存取權杖" if expected_type == "access" else "無效的刷新權杖",
        )
    try:
        token_break_glass_expiry = _break_glass_expiry_claim(payload)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="權杖的 break-glass 保證狀態無效",
        ) from exc
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="無效的存取權杖",
        )
    try:
        parsed_user_id = int(user_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="無效的存取權杖",
        ) from exc
    # W3-7f:`ui_settings` 是**每個認證請求**都會被拉出來的大 JSON 欄
    # (folders / convMeta / stars,上限 256KB),而 auth 熱路徑一個欄位都用不到它。
    # 這支是全平台每一個帶 token 的請求都會走的地方(`get_current_user` 與
    # `middleware/caller.py` 都呼叫它),所以那是白付的 wire + 反序列化成本。
    #
    # 用 `defer()` 而不是計畫建議的 `load_only()`:`load_only` 要把「需要的欄」
    # 逐一列出,而這個 User 物件下游用得很廣(username / role / clearance /
    # token_version / allowed_models…),漏一個就變成 N 次額外 SELECT,而且會在
    # 很遠的地方以難查的形式出現。`defer` 只針對那一個大欄,語意上是「先不要載」
    # —— 真的有人讀 `user.ui_settings`(例如 `GET /me/ui-settings`)時
    # SQLAlchemy 會自己補一次 SELECT,那條路徑本來就要讀它。
    user = (
        db.query(User)
        .options(defer(User.ui_settings))
        .filter(User.id == parsed_user_id)
        .first()
    )
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
    sid = payload.get("sid")
    jti = payload.get("jti")
    if (
        not isinstance(sid, str)
        or not isinstance(jti, str)
        or is_identifier_revoked(
            db,
            user_id=user.id,
            jti=jti,
            sid=sid,
            token_type=expected_type,
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="權杖或工作階段已撤銷，請重新登入",
        )
    session = db.get(AuthSession, sid) if isinstance(sid, str) else None
    if session is None:
        if not _sessionless_tokens_allowed():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="工作階段不存在，請重新登入",
            )
    else:
        if session.user_id != user.id or session.revoked_at is not None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="工作階段已撤銷，請重新登入",
            )
        try:
            durable_assurance = _assurance_from_session(session)
        except ValueError as exc:
            logger.warning("rejecting malformed durable auth session: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="工作階段保證狀態無效，請重新登入",
            ) from exc
        token_assurance = (
            tuple(payload.get("amr", ())),
            payload.get("acr"),
            payload.get("auth_time"),
            payload.get("break_glass"),
            payload.get("break_glass_ticket"),
            token_break_glass_expiry,
        )
        if token_assurance != durable_assurance:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="權杖與工作階段保證狀態不一致，請重新登入",
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
        # from a smart-card login.  A password-authenticated *current DB owner*
        # is admitted only while the named, time-bounded break-glass deployment
        # profile is active.  Deliberately do not trust the role copied into the
        # JWT: an old token must not retain owner assurance after DB demotion,
        # profile recovery, or incident-window expiry.
        card_assured = "sc" in methods
        owner_break_glass = (
            user.role == "owner"
            and "pwd" in methods
            and payload.get("break_glass") is True
            and payload.get("acr") == "urn:anila:acr:break-glass"
            and _active_break_glass_binding()
            == (
                payload.get("break_glass_ticket"),
                token_assurance[5],
            )
            and is_break_glass_active()
        )
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


def require_inference_audit_viewer(
    current_user: User = Depends(require_admin),
) -> User:
    """Inference audit list/export: owner always, else admin with grant.

    Plain admins without ``can_view_inference_audit`` get 403. IP /
    metadata redaction for non-owners remains separate (``is_owner``).
    """
    if is_owner(current_user) or bool(
        getattr(current_user, "can_view_inference_audit", False)
    ):
        return current_user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="需要推論稽核檢視授權",
    )


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
