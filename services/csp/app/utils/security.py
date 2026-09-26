"""密碼雜湊與 JWT 簽章／驗證。

簽章演算法固定 RS256，沒有 HS256 後援。私鑰放在資料庫的金鑰圈
（``app.services.jwt_keyring``），用 ``SECRET_KEY`` 衍生的憑證加密保存。
``SECRET_KEY`` 不拿來簽 access／refresh。JWKS 公布 next、active、retiring；
只拿 active 簽名。驗證只接受帶 kid、且該 kid 仍在公布集合裡的 RS256。
沒有 kid 的權杖一律拒絕。

``secrets/jwt-private.pem`` 只在金鑰圈還是空的時候匯入一次，之後不再讀。
檔案留著當備份，這個模組不會刪它。
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import bcrypt
from jose import jwt, JWTError


logger = logging.getLogger(__name__)


ALGORITHM: str = "RS256"
JWT_PRIVATE_KEY_PATH = "secrets/jwt-private.pem"
JWT_PUBLIC_KEY_PATH = "secrets/jwt-public.pem"


_BCRYPT_ROUNDS = 12
_BCRYPT_MAX_PASSWORD_BYTES = 72


def hash_password(password: str) -> str:
    if not isinstance(password, str):
        raise TypeError("password must be a string")
    # Restore passlib's historical 72-byte truncation; bcrypt 5.0.0 raises
    # instead, which would turn previously accepted long passwords into 500s.
    password_bytes = password.encode("utf-8")[:_BCRYPT_MAX_PASSWORD_BYTES]
    return bcrypt.hashpw(
        password_bytes,
        bcrypt.gensalt(rounds=_BCRYPT_ROUNDS),
    ).decode("ascii")


def verify_password(plain_password: str, hashed_password: str | None) -> bool:
    if not isinstance(plain_password, str):
        raise TypeError("password and hashed password must be strings")
    if hashed_password is None:
        return False
    if not isinstance(hashed_password, str):
        raise TypeError("password and hashed password must be strings")
    # Keep verification byte-for-byte symmetric with hash_password and passlib.
    password_bytes = plain_password.encode("utf-8")[:_BCRYPT_MAX_PASSWORD_BYTES]
    bcrypt_hash = hashed_password
    if hashed_password.startswith("$2$"):
        # bcrypt 5 rejects the legacy bare "$2$" identifier. Passlib's
        # fallback for backends without native "$2$" support repeated a
        # non-empty password to 72 bytes before using the compatible bcrypt
        # implementation; reproduce that input transformation before using
        # the accepted "$2b$" identifier. The 72-byte limit keeps the old
        # wraparound distinction unreachable.
        bcrypt_hash = "$2b$" + hashed_password[len("$2$"):]
        if password_bytes:
            repeat_count = (
                _BCRYPT_MAX_PASSWORD_BYTES + len(password_bytes) - 1
            ) // len(password_bytes)
            password_bytes = (
                password_bytes * repeat_count
            )[:_BCRYPT_MAX_PASSWORD_BYTES]
    return bcrypt.checkpw(
        password_bytes,
        bcrypt_hash.encode("ascii"),
    )


# ── Key loading ────────────────────────────────────────────────────────────────

class JwtKeyLoadError(RuntimeError):
    """Raised when the provisioned PEM file is missing or unreadable."""


def _resolve_key_path(raw: str) -> Path:
    """Resolve a fixed (possibly relative) PEM path.

    The fixed relative paths (``secrets/jwt-private.pem`` and
    ``secrets/jwt-public.pem``) keep docker mount points short and obvious.
    We resolve against the current working directory at import time — uvicorn
    and pytest both run from the backend root so this lines up. Absolute paths
    pass through unchanged.
    """
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _auto_generate_keypair(private_path: Path, public_path: Path) -> None:
    """Invoke ``scripts/generate-jwt-keypair.py`` to produce a fresh pair.

    This helper is used only by the pytest harness. Production key material
    is always provisioned by the deployment script. The subprocess approach
    keeps the keygen logic in one place
    and avoids importing the script's main() into runtime — the script
    has its own arg parser and exit codes we don't need here.
    """
    script_path = (
        Path(__file__).resolve().parent.parent.parent
        / "scripts"
        / "generate-jwt-keypair.py"
    )
    if not script_path.exists():
        raise JwtKeyLoadError(
            f"auto-keygen requested but script not found at {script_path}"
        )
    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--output-dir",
            str(private_path.parent),
            "--force",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise JwtKeyLoadError(
            "auto-keygen failed: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    # The script writes ``jwt-private.pem`` / ``jwt-public.pem`` —
    # rename if the caller asked for non-default filenames.
    expected_priv = private_path.parent / "jwt-private.pem"
    expected_pub = public_path.parent / "jwt-public.pem"
    if expected_priv != private_path and expected_priv.exists():
        expected_priv.rename(private_path)
    if expected_pub != public_path and expected_pub.exists():
        expected_pub.rename(public_path)


def _load_pem(path: Path, *, label: str) -> bytes:
    if not path.exists():
        # The remedy named here has to be one that works *where this error is
        # actually seen*, which is inside the container. Since 2026-08-06 the
        # runtime user is non-root (uid 10001) and ``/app/secrets`` is a
        # read-only mount, so runtime key generation is not an available
        # recovery path. Point at the host-side provisioning step instead.
        raise JwtKeyLoadError(
            f"JWT {label} key not found at {path}. "
            "簽章金鑰圈是空的，而且讀不到可匯入的 PEM。"
            "已上線的環境請還原資料庫備份，並使用同一把 SECRET_KEY 才能解開金鑰；"
            "不要重產一把新的金鑰來復原，那會讓既有登入與派工權杖全部失效。"
            "第一次啟動才需要主機上的 secrets/jwt-private.pem 與 jwt-public.pem。"
            "匯入之後這兩個檔只是備份，服務不會刪除它們，擁有者可以自行留著。"
            "容器內不能產鑰。"
        )
    try:
        return path.read_bytes()
    except PermissionError as exc:
        # The second failure mode this package can produce, and the one that
        # actually happens on a host where the keys were provisioned but the
        # alignment step was skipped: the file is right there, and we still
        # cannot read it. Without this branch the operator sees a bare
        # ``PermissionError`` with no pointer to the one command that fixes
        # it — indistinguishable from a corrupt deployment.
        raise JwtKeyLoadError(
            f"JWT {label} key exists at {path} but is not readable by this "
            f"process (uid {os.geteuid()}): {exc.strerror}. "
            "若資料庫裡已經有金鑰圈，這個 PEM 只是備份，讀不到也不影響簽名，服務不會刪除它。"
            "若這是第一次匯入，在主機執行 "
            "infra/deployment/scripts/fix-runtime-ownership.sh 後重啟，"
            "讓執行身分讀得到檔案（0640，不要改成全世界可讀）。"
            "還原已輪替的鑰匙要靠資料庫備份與同一把 SECRET_KEY，不是把 PEM 重產一次。"
        ) from exc


@lru_cache(maxsize=1)
def _load_keys() -> tuple[bytes, bytes]:
    """Read PEM files lazily on first use.

    Lazy load (vs module-import time) keeps key reads out of module import.
    The ``lru_cache`` ensures we hit the disk exactly once per process.
    Tests that need to swap keys mid-run monkeypatch the path constants and
    call ``_load_keys.cache_clear()``.
    """
    private_path = _resolve_key_path(JWT_PRIVATE_KEY_PATH)
    public_path = _resolve_key_path(JWT_PUBLIC_KEY_PATH)

    missing = not (private_path.exists() and public_path.exists())
    if missing and "PYTEST_CURRENT_TEST" in os.environ:
        logger.warning(
            "[security] pytest JWT keypair missing — generating at %s / %s",
            private_path,
            public_path,
        )
        _auto_generate_keypair(private_path, public_path)

    private_pem = _load_pem(private_path, label="private")
    public_pem = _load_pem(public_path, label="public")
    return private_pem, public_pem


def get_private_key(db=None) -> bytes:
    """目前 active 簽章鑰（PKCS#8 PEM）。金鑰圈是空的才會匯入 PEM。"""
    from app.services.jwt_keyring import active_private_pem

    return active_private_pem(db)


def get_public_key(db=None) -> bytes:
    """目前 active 的公鑰（SPKI PEM）。"""
    from app.services.jwt_keyring import public_pem_for_published_kid

    kid = get_kid(db)
    pem = public_pem_for_published_kid(kid, db)
    if pem is None:
        raise JwtKeyLoadError("金鑰圈沒有可公布的 active 公鑰")
    return pem


def _public_key_for_kid(kid: str | None, db=None) -> bytes | None:
    """依 kid 取仍可驗簽的公鑰。next、已退役、沒有 kid 都回 None。"""
    from app.services.jwt_keyring import verification_material

    if not kid:
        return None
    material = verification_material(kid, db)
    if material is None:
        return None
    return material.public_pem


def _claims_if_issuance_allowed(token: str, db, **decode_kwargs):
    """驗簽並套用簽發期間。失敗回 None。"""
    from app.services.jwt_keyring import issuance_allowed, verification_material

    try:
        header = jwt.get_unverified_header(token)
    except JWTError:
        return None
    if header.get("alg") != ALGORITHM:
        return None
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        return None
    material = verification_material(kid, db)
    if material is None:
        return None
    try:
        payload = jwt.decode(
            token,
            material.public_pem,
            algorithms=[ALGORITHM],
            **decode_kwargs,
        )
    except (JWTError, TypeError):
        return None
    if not issuance_allowed(material, payload):
        return None
    return payload


# ── Token signing ─────────────────────────────────────────────────────────────

def _jwt_headers(db=None) -> dict:
    """每張 CSP 簽的 JWT 都帶目前 active 的 kid。"""
    return {"kid": get_kid(db), "typ": "JWT"}


def _setting_at_issuance(db, key: str) -> int:
    """Resolve a token lifetime at the moment the token is signed."""

    from app.models.platform_setting import get_setting

    if db is not None:
        return int(get_setting(db, key))
    # Direct utility callers (mostly tests) use the same resolver rather than
    # reading the import-time Settings singleton.
    from app.database import SessionLocal

    with SessionLocal() as session:
        return int(get_setting(session, key))


def resolve_token_lifetimes(db=None) -> tuple[int, int]:
    """Resolve access minutes and refresh days for one token issuance."""
    return (
        _setting_at_issuance(db, "auth.access_token_expire_minutes"),
        _setting_at_issuance(db, "auth.refresh_token_expire_days"),
    )


def create_access_token(
    data: dict,
    db=None,
    *,
    lifetime_minutes: int | None = None,
) -> str:
    to_encode = data.copy()
    if lifetime_minutes is None:
        lifetime_minutes = _setting_at_issuance(
            db, "auth.access_token_expire_minutes"
        )
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=lifetime_minutes)
    to_encode.update({"exp": expire, "iat": int(now.timestamp()), "type": "access"})
    from app.services.jwt_keyring import active_signing_material

    material = active_signing_material(db)
    return jwt.encode(
        to_encode,
        material.private_pem,
        algorithm=ALGORITHM,
        headers={"kid": material.kid, "typ": "JWT"},
    )


def create_refresh_token(
    data: dict,
    db=None,
    *,
    lifetime_days: int | None = None,
) -> str:
    to_encode = data.copy()
    if lifetime_days is None:
        lifetime_days = _setting_at_issuance(
            db, "auth.refresh_token_expire_days"
        )
    now = datetime.now(timezone.utc)
    expire = now + timedelta(days=lifetime_days)
    to_encode.update({"exp": expire, "iat": int(now.timestamp()), "type": "refresh"})
    from app.services.jwt_keyring import active_signing_material

    material = active_signing_material(db)
    return jwt.encode(
        to_encode,
        material.private_pem,
        algorithm=ALGORITHM,
        headers={"kid": material.kid, "typ": "JWT"},
    )


# ── Token verification ────────────────────────────────────────────────────────

def decode_token(token: str, db=None) -> dict | None:
    """驗 RS256 權杖。失敗回 None，不丟例外給呼叫端。

    沒有 kid、kid 不是 active／retiring、演算法不是 RS256，都不放行。
    next 只公布、不驗簽。retiring 還要 iat 早於退役時間。
    ``algorithms=["RS256"]`` 再擋一次 alg=none／HS256。
    """
    if not token or not isinstance(token, str):
        return None
    return _claims_if_issuance_allowed(token, db)


def verify_token(token: str, db=None) -> dict | None:
    """``decode_token`` 的別名。"""
    return decode_token(token, db=db)


def get_kid(db=None) -> str:
    """目前拿來簽名的 kid。"""
    from app.services.jwt_keyring import active_kid

    return active_kid(db)


__all__ = [
    "ALGORITHM",
    "JwtKeyLoadError",
    "create_access_token",
    "create_refresh_token",
    "decode_token",
    "get_kid",
    "get_private_key",
    "get_public_key",
    "hash_password",
    "verify_password",
    "verify_token",
]
