# -*- coding: utf-8 -*-
"""CSP 簽章金鑰圈。

狀態：``next``（已公布、尚未簽名）→ ``active``（唯一簽名鑰）→
``retiring``（仍公布，讓未過期的登入與派工權杖繼續有效）→ ``retired``
（不再公布）。

排程跟內部服務憑證一樣：啟動先跑一次，之後週期執行。多個 worker
用 ``pg_advisory_xact_lock`` 串起來；SQLite 測試沒有這把鎖，靠
partial unique index 擋住第二把 active／next。

第一次沒有任何列時，把 ``secrets/jwt-private.pem`` 以 ``settings.JWT_KID``
匯入成 active。匯入後簽名不再讀 PEM。檔案留在原處當備份，這裡不刪。
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from anila_core.security.credential_crypto import decrypt_credential, encrypt_credential

from app.config import settings
from app.models.jwt_signing_key import JwtSigningKey
from app.time_utils import as_utc, utcnow

logger = logging.getLogger(__name__)

# 與 JWKS ``Cache-Control: max-age`` 同一數字。next 至少先公布這麼久才升 active；
# retiring 比 refresh token 允許的最長期限再多這一段。
JWKS_CACHE_MAX_AGE_SECONDS = 3600
JWT_KEYRING_MAINTAIN_INTERVAL_SECONDS = 3600
_MIN_MAINTAIN_INTERVAL_SECONDS = 60

# 0x4A57544B = "JWTK"。交易結束 Postgres 會放鎖。不是祕密。
JWT_KEYRING_ADVISORY_LOCK_KEY = 0x4A57544B

ROTATION_DAYS_KEY = "auth.jwt_rotation_days"
EMERGENCY_CONFIRM_TEXT = (
    "所有人會被登出，進行中的派工權杖會失效。"
    "自行驗證派工權杖的 agent 最多還能接受舊鑰 5 分鐘"
)

STATE_NEXT = "next"
STATE_ACTIVE = "active"
STATE_RETIRING = "retiring"
STATE_RETIRED = "retired"
PUBLISHED_STATES = (STATE_NEXT, STATE_ACTIVE, STATE_RETIRING)

_LOCK = threading.RLock()
# 同一把 PEM 重複密封時沿用同一組密文，避免每個測試都再跑一次 PBKDF2。
# 新產生的金鑰每次內容都不同，不會撞到這個快取。
_seal_cache: dict[bytes, tuple[bytes, bytes, bytes]] = {}
_opened: dict[tuple[bytes, bytes, bytes], bytes] = {}


@dataclass(frozen=True)
class EmergencyRotation:
    kid: str
    retired_kids: tuple[str, ...]


@dataclass(frozen=True)
class SigningMaterial:
    kid: str
    private_pem: bytes


@dataclass(frozen=True)
class VerificationMaterial:
    public_pem: bytes
    state: str
    retiring_at: datetime | None
    accept_missing_iat: bool


@dataclass(frozen=True)
class PublishedJwk:
    kid: str
    public_pem: str
    state: str
    accept_missing_iat: bool
    iat_not_after: int | None


def maintainer_enabled() -> bool:
    """未設定視為開啟。測試用 ``ANILA_JWT_KEYRING_MAINTAINER=0`` 關掉。"""
    raw = os.environ.get("ANILA_JWT_KEYRING_MAINTAINER")
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _maintain_interval_seconds() -> int:
    return max(int(JWT_KEYRING_MAINTAIN_INTERVAL_SECONDS), _MIN_MAINTAIN_INTERVAL_SECONDS)


def acquire_jwt_keyring_lock(db: Session) -> None:
    """PostgreSQL 取交易級 advisory lock；其他方言不做。"""
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    db.execute(
        text("SELECT pg_advisory_xact_lock(:k)"),
        {"k": JWT_KEYRING_ADVISORY_LOCK_KEY},
    )


def _open(db: Session | None) -> tuple[Session, bool]:
    if db is not None:
        return db, False
    from app.database import SessionLocal

    return SessionLocal(), True


def _one(db: Session, state: str) -> JwtSigningKey | None:
    return db.query(JwtSigningKey).filter(JwtSigningKey.state == state).one_or_none()


def _rows(db: Session, state: str) -> list[JwtSigningKey]:
    return db.query(JwtSigningKey).filter(JwtSigningKey.state == state).all()


def publish_lead(period: timedelta) -> timedelta:
    """next 要在升 active 之前先公布。90 天週期提前 7 天，且不得短於一個快取週期。"""
    cache = timedelta(seconds=JWKS_CACHE_MAX_AGE_SECONDS)
    half = period / 2
    week = timedelta(days=7)
    well = half if half < week else week
    if well < cache:
        return cache
    return well


def retiring_window() -> timedelta:
    """比 refresh token 允許的最長天數再多一個 JWKS 快取週期。

    用登錄表的上限，不用當下設定值。管理員把天數調低之後，
    先前以較長期限發出的 refresh token 仍須驗得過。
    """
    from app.services.settings_registry import require_spec

    spec = require_spec("auth.refresh_token_expire_days")
    max_days = int(spec.domain_fn.bounds[1])
    return timedelta(days=max_days) + timedelta(seconds=JWKS_CACHE_MAX_AGE_SECONDS)


def _rotation_period(db: Session) -> timedelta:
    from app.models.platform_setting import get_setting

    return timedelta(days=int(get_setting(db, ROTATION_DAYS_KEY)))


def _seal(private_pem: bytes) -> tuple[bytes, bytes, bytes]:
    cached = _seal_cache.get(private_pem)
    if cached is not None:
        return cached
    sealed = encrypt_credential(private_pem.decode("utf-8"))
    _seal_cache[private_pem] = sealed
    return sealed


def _open_private(row: JwtSigningKey) -> bytes:
    seal = (
        bytes(row.private_ciphertext),
        bytes(row.private_nonce),
        bytes(row.private_tag),
    )
    cached = _opened.get(seal)
    if cached is not None:
        return cached
    pem = decrypt_credential(*seal).encode("utf-8")
    _opened[seal] = pem
    return pem


def _generate_rsa() -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def _read_bootstrap_pems() -> tuple[bytes, bytes]:
    from app.utils import security as sec

    private_pem, public_pem = sec._load_keys()
    private_key = serialization.load_pem_private_key(private_pem, password=None)
    public_key = serialization.load_pem_public_key(public_pem)
    if not isinstance(private_key, RSAPrivateKey) or not isinstance(public_key, RSAPublicKey):
        raise sec.JwtKeyLoadError("JWT PEM 不是 RSA 金鑰")
    if private_key.public_key().public_numbers() != public_key.public_numbers():
        raise sec.JwtKeyLoadError("jwt-private.pem 與 jwt-public.pem 不是一對")
    return private_pem, public_pem


def _fresh_kid(db: Session) -> str:
    for _ in range(5):
        kid = "anila-" + secrets.token_hex(8)
        if db.query(JwtSigningKey.id).filter(JwtSigningKey.kid == kid).first() is None:
            return kid
    raise RuntimeError("無法產生不重複的 kid")


def _insert_row(
    db: Session,
    *,
    kid: str,
    state: str,
    private_pem: bytes,
    public_pem: bytes,
    created_at: datetime,
    activated_at: datetime | None,
    accept_missing_iat: bool = False,
) -> JwtSigningKey:
    ciphertext, nonce, tag = _seal(private_pem)
    row = JwtSigningKey(
        kid=kid,
        state=state,
        private_ciphertext=ciphertext,
        private_nonce=nonce,
        private_tag=tag,
        public_pem=public_pem.decode("ascii"),
        created_at=created_at,
        activated_at=activated_at,
        accept_missing_iat=accept_missing_iat,
    )
    db.add(row)
    db.flush()
    return row


def _import_pem_as_active(db: Session, now: datetime) -> JwtSigningKey:
    private_pem, public_pem = _read_bootstrap_pems()
    # 不刪 PEM。擁有者可以留著當最初那把鑰匙的備份。
    return _insert_row(
        db,
        kid=settings.JWT_KID,
        state=STATE_ACTIVE,
        private_pem=private_pem,
        public_pem=public_pem,
        created_at=now,
        activated_at=now,
        accept_missing_iat=True,
    )


def ensure_bootstrapped(db: Session, now: datetime | None = None) -> bool:
    """沒有任何列時匯入 PEM 成 active。已有 active 則不動、也不拿鎖。回傳是否新插入。"""
    moment = as_utc(now) or utcnow()
    if _one(db, STATE_ACTIVE) is not None:
        return False
    with _LOCK:
        acquire_jwt_keyring_lock(db)
        if _one(db, STATE_ACTIVE) is not None:
            return False
        if db.query(JwtSigningKey.id).count():
            from app.utils.security import JwtKeyLoadError

            raise JwtKeyLoadError(
                "金鑰圈沒有 active 金鑰，但已有其他列。"
                "請還原資料庫備份（同一把 SECRET_KEY），或由擁有者／管理員做緊急輪替。"
                "不要重產 PEM；服務不會刪除既有的 PEM 備份。"
            )
        try:
            with db.begin_nested():
                _import_pem_as_active(db, moment)
        except IntegrityError:
            if _one(db, STATE_ACTIVE) is not None:
                return False
            raise
        logger.info("JWT 金鑰圈已從 PEM 匯入 active kid=%s", settings.JWT_KID)
        return True


def _insert_generated(db: Session, now: datetime, *, state: str) -> JwtSigningKey:
    private_pem, public_pem = _generate_rsa()
    return _insert_row(
        db,
        kid=_fresh_kid(db),
        state=state,
        private_pem=private_pem,
        public_pem=public_pem,
        created_at=now,
        activated_at=now if state == STATE_ACTIVE else None,
    )


def advance_jwt_keyring(db: Session, now: datetime | None = None) -> list[str]:
    """往前推一格排程。同一個 tick 新建立的 next 不會立刻升 active。"""
    moment = as_utc(now) or utcnow()
    actions: list[str] = []
    with _LOCK:
        acquire_jwt_keyring_lock(db)
        if ensure_bootstrapped(db, moment):
            actions.append("bootstrap")
        active = _one(db, STATE_ACTIVE)
        if active is None:
            from app.utils.security import JwtKeyLoadError

            raise JwtKeyLoadError("金鑰圈沒有 active 金鑰，無法排程輪替")
        period = _rotation_period(db)
        lead = publish_lead(period)
        cache = timedelta(seconds=JWKS_CACHE_MAX_AGE_SECONDS)
        nxt = _one(db, STATE_NEXT)
        active_for = moment - (as_utc(active.activated_at) or as_utc(active.created_at))
        if nxt is None and active_for >= (period - lead):
            nxt = _insert_generated(db, moment, state=STATE_NEXT)
            actions.append("create_next")
            logger.info("JWT 金鑰圈建立 next kid=%s", nxt.kid)
        if nxt is not None and active_for >= period:
            published_for = moment - (as_utc(nxt.created_at) or moment)
            if published_for >= cache:
                # 先把舊的 active 改成 retiring 並 flush，避免兩列同時是 active
                # 撞上 partial unique index。
                previous_kid = active.kid
                active.state = STATE_RETIRING
                active.retiring_at = moment
                db.flush()
                nxt.state = STATE_ACTIVE
                nxt.activated_at = moment
                db.flush()
                logger.info(
                    "JWT 金鑰圈升 active kid=%s，上一把 %s 進入 retiring",
                    nxt.kid,
                    previous_kid,
                )
                actions.append("promote")
        window = retiring_window()
        for row in list(_rows(db, STATE_RETIRING)):
            started = as_utc(row.retiring_at)
            if started is not None and moment >= started + window:
                row.state = STATE_RETIRED
                row.retired_at = moment
                logger.info("JWT 金鑰圈退役 kid=%s", row.kid)
                actions.append("retire")
        db.flush()
    return actions


def emergency_rotate(db: Session, now: datetime | None = None) -> EmergencyRotation:
    """立刻啟用新鑰，其餘已公布的鑰匙全部退役、不再公布。"""
    moment = as_utc(now) or utcnow()
    with _LOCK:
        acquire_jwt_keyring_lock(db)
        if db.query(JwtSigningKey.id).count() == 0:
            try:
                ensure_bootstrapped(db, moment)
            except Exception as exc:
                from app.utils.security import JwtKeyLoadError

                if not isinstance(exc, JwtKeyLoadError):
                    raise
        retired: list[str] = []
        for row in db.query(JwtSigningKey).all():
            if row.state == STATE_RETIRED:
                continue
            retired.append(row.kid)
            row.state = STATE_RETIRED
            row.retired_at = moment
        db.flush()
        fresh = _insert_generated(db, moment, state=STATE_ACTIVE)
        db.flush()
        logger.info(
            "JWT 金鑰圈緊急輪替 new=%s retired=%s",
            fresh.kid,
            ",".join(retired),
        )
        return EmergencyRotation(kid=fresh.kid, retired_kids=tuple(retired))


def published_rows(db: Session) -> list[JwtSigningKey]:
    rows = (
        db.query(JwtSigningKey)
        .filter(JwtSigningKey.state.in_(PUBLISHED_STATES))
        .all()
    )
    rank = {STATE_ACTIVE: 0, STATE_NEXT: 1, STATE_RETIRING: 2}
    rows.sort(key=lambda row: (rank.get(row.state, 9), row.id or 0))
    return rows


def _iat_not_after(row: JwtSigningKey) -> int | None:
    if row.state != STATE_RETIRING:
        return None
    moment = as_utc(row.retiring_at)
    if moment is None:
        return None
    return int(moment.timestamp())


def _published_jwk(row: JwtSigningKey) -> PublishedJwk:
    return PublishedJwk(
        kid=row.kid,
        public_pem=row.public_pem,
        state=row.state,
        accept_missing_iat=bool(row.accept_missing_iat),
        iat_not_after=_iat_not_after(row),
    )


def jwks_material(db: Session | None = None) -> tuple[list[PublishedJwk], bool]:
    """回傳 ``([公布中的公鑰, ...], 這次是否新匯入)``。已有 active 時不拿鎖。"""
    session, owned = _open(db)
    try:
        inserted = ensure_bootstrapped(session)
        material = [_published_jwk(row) for row in published_rows(session)]
        if inserted and owned:
            session.commit()
        elif inserted:
            session.flush()
        return material, inserted
    finally:
        if owned:
            session.close()


def active_private_pem(db: Session | None = None) -> bytes:
    session, owned = _open(db)
    try:
        inserted = ensure_bootstrapped(session)
        row = _one(session, STATE_ACTIVE)
        if row is None:
            from app.utils.security import JwtKeyLoadError

            raise JwtKeyLoadError("金鑰圈沒有 active 金鑰")
        pem = _open_private(row)
        if inserted and owned:
            session.commit()
        elif inserted:
            session.flush()
        return pem
    finally:
        if owned:
            session.close()


def active_kid(db: Session | None = None) -> str:
    session, owned = _open(db)
    try:
        inserted = ensure_bootstrapped(session)
        row = _one(session, STATE_ACTIVE)
        if row is None:
            from app.utils.security import JwtKeyLoadError

            raise JwtKeyLoadError("金鑰圈沒有 active 金鑰")
        kid = row.kid
        if inserted and owned:
            session.commit()
        elif inserted:
            session.flush()
        return kid
    finally:
        if owned:
            session.close()


def _finish(session: Session, owned: bool, inserted: bool) -> None:
    if inserted and owned:
        session.commit()
    elif inserted:
        session.flush()


def active_signing_material(db: Session | None = None) -> SigningMaterial:
    """同一筆交易讀出 active 的 kid 與私鑰。簽章只能用這一對。"""
    session, owned = _open(db)
    try:
        row = _one(session, STATE_ACTIVE)
        inserted = False
        if row is None:
            inserted = ensure_bootstrapped(session)
            row = _one(session, STATE_ACTIVE)
        if row is None:
            from app.utils.security import JwtKeyLoadError

            raise JwtKeyLoadError("金鑰圈沒有 active 金鑰")
        material = SigningMaterial(kid=row.kid, private_pem=_open_private(row))
        _finish(session, owned, inserted)
        return material
    finally:
        if owned:
            session.close()


def issuance_allowed(material: VerificationMaterial, payload: dict) -> bool:
    """next 不驗。retiring 只收 retiring_at 之前簽的。沒有 iat 只收匯入的那一把。"""
    if material.state == STATE_NEXT or material.state not in (STATE_ACTIVE, STATE_RETIRING):
        return False
    iat = payload.get("iat")
    if iat is None:
        return material.accept_missing_iat
    if isinstance(iat, bool):
        return False
    try:
        issued = int(iat)
    except (TypeError, ValueError):
        return False
    if material.state != STATE_RETIRING:
        return True
    moment = as_utc(material.retiring_at)
    if moment is None:
        return False
    return issued < int(moment.timestamp())


def verification_material(kid: str, db: Session | None = None) -> VerificationMaterial | None:
    """驗簽只看 active 與 retiring。next 公布但不接受。已有 active 時不拿鎖。"""
    if not isinstance(kid, str) or not kid:
        return None
    session, owned = _open(db)
    try:
        inserted = False

        def _row() -> JwtSigningKey | None:
            return (
                session.query(JwtSigningKey)
                .filter(
                    JwtSigningKey.kid == kid,
                    JwtSigningKey.state.in_((STATE_ACTIVE, STATE_RETIRING)),
                )
                .one_or_none()
            )

        row = _row()
        if row is None and _one(session, STATE_ACTIVE) is None:
            try:
                inserted = ensure_bootstrapped(session)
            except Exception as exc:
                from app.utils.security import JwtKeyLoadError

                if isinstance(exc, JwtKeyLoadError):
                    return None
                raise
            row = _row()
        _finish(session, owned, inserted)
        if row is None or not row.public_pem:
            return None
        return VerificationMaterial(
            public_pem=row.public_pem.encode("utf-8"),
            state=row.state,
            retiring_at=row.retiring_at,
            accept_missing_iat=bool(row.accept_missing_iat),
        )
    finally:
        if owned:
            session.close()


def assert_active_key_decryptable(db: Session | None = None) -> None:
    """解不開 active 私鑰就拒絕啟動。不走程序內解密快取，避免舊密文被當成還解得開。"""
    session, owned = _open(db)
    try:
        if session.query(JwtSigningKey.id).count() == 0:
            return
        row = _one(session, STATE_ACTIVE)
        from app.utils.security import JwtKeyLoadError

        if row is None:
            raise JwtKeyLoadError(
                "金鑰圈沒有 active 金鑰。請還原資料庫備份，或執行 "
                "infra/deployment/scripts/reseal-credentials.py。"
            )
        try:
            decrypt_credential(
                bytes(row.private_ciphertext),
                bytes(row.private_nonce),
                bytes(row.private_tag),
            )
        except Exception as exc:
            raise JwtKeyLoadError(
                "無法解密 active 簽章私鑰。更換 SECRET_KEY 之前請先執行 "
                "infra/deployment/scripts/reseal-credentials.py --apply，"
                "用舊密鑰把 jwt_signing_keys 轉封到新密鑰。"
            ) from exc
    finally:
        if owned:
            session.close()


def public_pem_for_published_kid(kid: str, db: Session | None = None) -> bytes | None:
    """只接受目前公布的 kid。沒有 kid、已退役、或金鑰圈還空著且 PEM 也沒有，回 None。"""
    if not isinstance(kid, str) or not kid:
        return None
    session, owned = _open(db)
    try:
        try:
            inserted = ensure_bootstrapped(session)
        except Exception as exc:
            from app.utils.security import JwtKeyLoadError

            if isinstance(exc, JwtKeyLoadError):
                return None
            raise
        row = (
            session.query(JwtSigningKey)
            .filter(
                JwtSigningKey.kid == kid,
                JwtSigningKey.state.in_(PUBLISHED_STATES),
            )
            .one_or_none()
        )
        if inserted and owned:
            session.commit()
        elif inserted:
            session.flush()
        if row is None or not row.public_pem:
            return None
        return row.public_pem.encode("utf-8")
    finally:
        if owned:
            session.close()


def maintain_jwt_keyring_once(*, strict: bool = False) -> None:
    """跑一輪排程並提交。關閉時直接返回。``strict`` 失敗時往上拋。"""
    if not maintainer_enabled():
        return
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        advance_jwt_keyring(db)
        db.commit()
    except Exception:
        logger.exception("JWT 金鑰圈維護失敗")
        try:
            db.rollback()
        except Exception:
            pass
        if strict:
            raise
    finally:
        db.close()


async def start_jwt_keyring_maintainer() -> asyncio.Task:
    """週期維護。呼叫端應先同步跑過一輪，這個 task 先睡再跑。"""

    async def _loop() -> None:
        while True:
            await asyncio.sleep(_maintain_interval_seconds())
            try:
                maintain_jwt_keyring_once()
            except Exception:
                logger.exception("JWT 金鑰圈排程失敗")

    logger.info("JWT 金鑰圈排程每 %s 秒檢查一次", _maintain_interval_seconds())
    return asyncio.create_task(_loop(), name="jwt-keyring-maintainer")


def reset_stored_keys_for_tests() -> None:
    """清掉測試資料庫裡的簽章列。不刪 PEM，也不清解密快取。"""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        db.query(JwtSigningKey).delete()
        db.commit()
    finally:
        db.close()
