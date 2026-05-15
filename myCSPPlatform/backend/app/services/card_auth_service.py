"""中科院憑證卡登入：HTTP layer 用的 service orchestration。

本檔包三件事：

1. **Challenge 簽發 / 驗證** — 用 ``SECRET_KEY`` 簽一條 ``aud="card-challenge"``、
   ``exp=2min`` 的 JWT，內含 ``nonce``。Pattern 對齊
   ``external_auth_service.issue_external_state`` 的 OIDC state JWT，**stateless**，
   不依賴 Redis / DB nonce store。
2. **PKCS#7 簽章驗證** — 委派給 ``card_auth.verify_pkcs7_signature``（純函式）。
3. **User get-or-create** — ``username = employee_id``；無對應使用者時自動建立，
   ``local_password_disabled=True`` 確保卡片帳號無法走本地登入後門。

不在本檔處理的事：
- **Cookie 設定 / TokenResponse 組裝** — 留給 ``api/auth.py`` 的 endpoint 接
  ``_finalize_login`` 處理，跟 OIDC callback 一致。
- **HTTP layer error mapping** — 本檔 raise 業務例外（``CardAuthError`` /
  ``CardLoginRejected``），endpoint 負責轉成 HTTPException。
"""
from __future__ import annotations

import base64
import logging
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives.serialization import pkcs7 as crypto_pkcs7
from jose import jwt as jose_jwt
from jose.exceptions import JWTError
from sqlalchemy.orm import Session

from app.config import settings
from app.models.user import User
from app.services.card_auth import (
    CardAuthError,
    CardClaims,
    InvalidSignatureError,
    VerifyMode,
    _DEFAULT_CA_BUNDLE_PATH,
    _load_ca_bundle,
    _pick_signer_cert,
    verify_pkcs7_signature,
)
from app.services.card_revocation import (
    CertificateRevokedError,
    OcspUnavailableError,
    OcspUnknownError,
    check_ocsp,
)
from app.utils.security import hash_password


logger = logging.getLogger(__name__)


CARD_CHALLENGE_AUDIENCE = "card-challenge"
CHALLENGE_TTL_SECONDS = 120

# Pending users 完成註冊用的 short-lived JWT。比 challenge JWT 久 (使用者
# 可能要花幾分鐘在 dropdown 找單位)，但仍短到攻擊者抓到也活不久。
CARD_REGISTRATION_AUDIENCE = "card-registration"
REGISTRATION_TTL_SECONDS = 900  # 15 分鐘


class CardLoginRejected(Exception):
    """卡片身分合法但拒絕登入（例：email 與既有帳號衝突）。

    跟 ``CardAuthError``（簽章層級失敗）分開，endpoint 可對應到不同 HTTP status。
    """


class CardRegistrationTokenInvalid(Exception):
    """``registration_token`` 過期 / 簽章不對 / 對應使用者不存在。"""


# ─── Challenge 簽發 / 驗證 ─────────────────────────────────────────────────────


def issue_card_challenge() -> tuple[str, str, int]:
    """簽發新的 challenge。

    Returns:
        ``(challenge_token, nonce, expires_in)``：``challenge_token`` 為 JWT
        字串、``nonce`` 為明文（給 client 拿去簽）、``expires_in`` 為剩餘秒數。
    """
    nonce = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    exp = now + timedelta(seconds=CHALLENGE_TTL_SECONDS)
    payload = {
        "aud": CARD_CHALLENGE_AUDIENCE,
        "nonce": nonce,
        "iat": now,
        "exp": exp,
    }
    challenge_token = jose_jwt.encode(
        payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM
    )
    return challenge_token, nonce, CHALLENGE_TTL_SECONDS


def decode_card_challenge(challenge_token: str) -> str:
    """驗 challenge JWT 並取出 nonce。

    Raises:
        CardLoginRejected: JWT 過期 / 簽章不對 / aud 錯 / 缺 nonce。
    """
    try:
        payload = jose_jwt.decode(
            challenge_token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
            audience=CARD_CHALLENGE_AUDIENCE,
        )
    except JWTError as exc:
        raise CardLoginRejected(f"challenge_token 無效或過期: {exc}") from exc

    nonce = payload.get("nonce")
    if not nonce:
        raise CardLoginRejected("challenge_token 缺 nonce")
    return nonce


# ─── Registration token：給 pending 使用者完成註冊用 ────────────────────────────


def issue_registration_token(user_id: int) -> tuple[str, int]:
    """簽發 short-lived registration JWT 讓 pending 使用者完成註冊。

    JWT 內只放 user_id；audience=``card-registration`` 跟 challenge token
    區分，避免兩個 token 互相代用。

    Returns:
        ``(token, expires_in)``：JWT 字串 + 剩餘秒數。
    """
    now = datetime.now(timezone.utc)
    payload = {
        "aud": CARD_REGISTRATION_AUDIENCE,
        "uid": user_id,
        "iat": now,
        "exp": now + timedelta(seconds=REGISTRATION_TTL_SECONDS),
    }
    token = jose_jwt.encode(
        payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM
    )
    return token, REGISTRATION_TTL_SECONDS


def decode_registration_token(token: str) -> int:
    """驗 registration JWT 並取出 user_id。

    Raises:
        CardRegistrationTokenInvalid: 簽章 / aud / exp 任一驗失敗。
    """
    try:
        payload = jose_jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
            audience=CARD_REGISTRATION_AUDIENCE,
        )
    except JWTError as exc:
        raise CardRegistrationTokenInvalid(
            f"registration_token 無效或過期: {exc}"
        ) from exc

    user_id = payload.get("uid")
    if not isinstance(user_id, int):
        raise CardRegistrationTokenInvalid("registration_token 缺 uid")
    return user_id


# ─── 主流程：驗證 → get-or-create user ─────────────────────────────────────────


def verify_card_and_resolve_user(
    db: Session,
    *,
    signature_b64: str,
    challenge_token: str,
    card_serial: str | None,
) -> tuple[User, CardClaims]:
    """卡片登入主流程。

    1. 驗 challenge_token JWT 還原 nonce。
    2. 驗 PKCS#7 簽章對 nonce + 抽 cert claims（mode 由 ``settings.CARD_VERIFY_MODE``）。
    3. ``username = employee_id`` 查 user，找不到就 auto-provision。
    4. 同步 email（若 cert 有 email 但 user.email 不一致時更新；衝突則拒絕）。

    Returns:
        ``(user, claims)``：``claims`` 也回傳給 endpoint 寫 audit log /
        UI flash message 用。

    Raises:
        CardAuthError: 簽章本身解析 / 驗證失敗（→ HTTP 401）。
        CardLoginRejected: challenge 無效、或 email 與既有帳號衝突（→ HTTP 4xx）。
    """
    nonce = decode_card_challenge(challenge_token)

    mode = _resolve_verify_mode(settings.CARD_VERIFY_MODE)
    ca_bundle = _resolve_ca_bundle_path()
    claims = verify_pkcs7_signature(
        signature_b64=signature_b64,
        expected_tbs=nonce,
        mode=mode,
        card_serial=card_serial,
        ca_bundle_path=ca_bundle,
    )

    if mode is VerifyMode.STRICT and settings.CARD_CHECK_REVOCATION:
        _run_revocation_check(signature_b64, ca_bundle)

    user = _get_or_create_card_user(db, claims)
    return user, claims


# ─── Internal helpers ──────────────────────────────────────────────────────────


def _resolve_ca_bundle_path() -> Path:
    """設定優先序：``settings.NCSIST_CA_BUNDLE_PATH`` > default bundle。"""
    if settings.NCSIST_CA_BUNDLE_PATH:
        return Path(settings.NCSIST_CA_BUNDLE_PATH)
    return _DEFAULT_CA_BUNDLE_PATH


def _run_revocation_check(signature_b64: str, ca_bundle_path: Path) -> None:
    """STRICT mode 啟用 OCSP 時的撤銷檢查。

    重新 decode PKCS#7 抽出 signer + issuer cert（issuer 從 PKCS#7 內含 cert
    + CA bundle 的 intermediates 內 match subject == signer.issuer 找到）。
    Decode 兩次的開銷可忽略：每次登入只一次，且 cert parse 是 ms 級。

    fail-close policy：OCSP unknown / unavailable / revoked 全部都拒絕登入。
    OCSP 之所以是「強化機制」，是因為它的存在意義就是 detect 「cert 在 valid
    period 內但被 revoke」這個 chain 驗證做不到的 case；不能 detect 就要拒。
    """
    try:
        der = base64.b64decode(signature_b64, validate=True)
        embedded = crypto_pkcs7.load_der_pkcs7_certificates(der)
    except (ValueError, OSError) as exc:
        raise CardLoginRejected(f"OCSP 階段重新解析簽章失敗: {exc}") from exc

    signer = _pick_signer_cert(embedded)
    _, intermediates_from_bundle = _load_ca_bundle(ca_bundle_path)
    candidates = list(embedded) + intermediates_from_bundle
    issuer = next(
        (c for c in candidates if c.subject == signer.issuer), None
    )
    if issuer is None:
        raise CardLoginRejected(
            f"找不到 signer 的 issuer cert (subject={signer.issuer.rfc4514_string()})，"
            "OCSP 無法執行"
        )

    try:
        check_ocsp(signer, issuer)
    except CertificateRevokedError as exc:
        logger.warning(
            "card_auth OCSP revoked: serial=%s revoked_at=%s",
            f"{signer.serial_number:x}",
            exc.revoked_at,
        )
        raise CardLoginRejected(f"憑證已被撤銷: {exc}") from exc
    except (OcspUnknownError, OcspUnavailableError) as exc:
        logger.warning("card_auth OCSP fail-close: %s", exc)
        raise CardLoginRejected(f"OCSP 撤銷狀態無法確認: {exc}") from exc


def _resolve_verify_mode(raw: str) -> VerifyMode:
    """settings.CARD_VERIFY_MODE 解析。預設 LOOSE（dev）；prod 必填 'strict'。"""
    normalized = (raw or "loose").lower()
    try:
        return VerifyMode(normalized)
    except ValueError as exc:
        raise CardLoginRejected(
            f"CARD_VERIFY_MODE 設定無效: {raw!r}（必須是 'loose' 或 'strict'）"
        ) from exc


def _parse_initial_owners() -> set[str]:
    """從 ``settings.CARD_INITIAL_OWNERS`` 解析出員工編號集合。

    支援 CSV 格式（``"1147259,1090868"``），allow whitespace。空字串回空集合。
    """
    raw = settings.CARD_INITIAL_OWNERS or ""
    return {part.strip() for part in raw.split(",") if part.strip()}


def _get_or_create_card_user(db: Session, claims: CardClaims) -> User:
    """以 ``username = employee_id`` 查既有 user，找不到就建立新帳號。

    Provisioning policy（branch SSO）：
    - 列在 ``CARD_INITIAL_OWNERS`` env 內 → ``role="owner"`` + ``is_approved=True``
      （bootstrap：解決「沒有 owner 沒人能 approve」的死結）。
    - 其他人 → ``role="user"`` + ``is_approved=False``，下游 endpoint 不種
      cookie session、改回 pending 狀態，使用者必須完成註冊（填單位）等
      admin 核准後下次刷卡才能真登入。

    email collision policy 對齊 OIDC ``_provision_external_user``：若 email
    已綁到「不同 employee_id」的帳號，拒絕自動接管，要 admin 手動處理。
    """
    user = db.query(User).filter(User.username == claims.employee_id).first()
    if user:
        return _sync_existing_card_user(db, user, claims)

    if claims.email:
        existing_email_user = (
            db.query(User).filter(User.email == claims.email).first()
        )
        if existing_email_user is not None:
            raise CardLoginRejected(
                f"email「{claims.email}」已綁定其他帳號"
                f"（username={existing_email_user.username}），"
                "為避免帳號接管風險，自動建立已停用，請聯絡 admin 手動處理。"
            )

    is_initial_owner = claims.employee_id in _parse_initial_owners()
    user = User(
        username=claims.employee_id,
        email=claims.email,
        # 卡片帳號永遠不走本地登入；hashed_password 塞 unguessable random，
        # 配合 local_password_disabled=True 雙重保險。
        hashed_password=hash_password(secrets.token_urlsafe(32)),
        role="owner" if is_initial_owner else "user",
        is_active=True,
        is_approved=is_initial_owner,
        local_password_disabled=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info(
        "card_auth provisioned new user: id=%s username=%s email=%s "
        "role=%s is_approved=%s (initial_owner=%s)",
        user.id,
        user.username,
        user.email,
        user.role,
        user.is_approved,
        is_initial_owner,
    )
    return user


def _sync_existing_card_user(
    db: Session, user: User, claims: CardClaims
) -> User:
    """既有卡片帳號：email 若有變更則更新；衝突則拒絕（保護其他帳號）。"""
    if not claims.email or user.email == claims.email:
        return user

    collision = (
        db.query(User)
        .filter(User.email == claims.email, User.id != user.id)
        .first()
    )
    if collision is not None:
        raise CardLoginRejected(
            f"卡片回傳 email「{claims.email}」已綁到另一個帳號"
            f"（username={collision.username}），拒絕更新以避免帳號接管。"
        )

    user.email = claims.email
    db.commit()
    db.refresh(user)
    return user
