"""Password hashing + JWT signing/verification utilities.

Sprint 9 / anila-studio extraction: JWT algorithm switched from
symmetric HS256 to asymmetric RS256. CSP holds the private key (PKCS#8
PEM at ``settings.JWT_PRIVATE_KEY_PATH``) and signs access/refresh
tokens; anila-studio (and any future downstream verifier) fetches the
matching public key from ``GET /.well-known/jwks.json`` and verifies
locally — no shared secret crosses the trust boundary.

Cutover notes:

* No HS256 fallback. Existing tokens issued under HS256 are invalidated
  on deploy; users re-authenticate once.
* ``ALGORITHM`` constant is hard-coded to ``"RS256"``. ``settings.ALGORITHM``
  is no longer consulted here (kept on Settings for legacy / observability).
* ``settings.SECRET_KEY`` is NOT used for JWT in the RS256 path. It is
  retained only because ``startup_security`` / ``credential_crypto`` /
  audit logging still depend on it for non-JWT purposes.
* ``jwt.decode`` is always called with an explicit ``algorithms=["RS256"]``
  allowlist, so a token with ``alg=none`` or ``alg=HS256`` is rejected
  before the verifier ever touches the key — eliminating the classic
  algorithm-confusion attack against systems that previously accepted HS256.
* JWT header carries ``kid`` so the verifier can pick the right public
  key from JWKS. ``kid`` must match ``settings.JWT_KID`` to validate;
  tokens with missing or unknown ``kid`` are rejected.
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

from app.config import settings


logger = logging.getLogger(__name__)


ALGORITHM: str = "RS256"


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
    """Raised at import time when the configured PEM file is missing
    and ``ALLOW_AUTO_KEYGEN`` is False. Surfaces a clear actionable
    message so operators know exactly which step they skipped."""


def _resolve_key_path(raw: str) -> Path:
    """Resolve a (possibly relative) configured PEM path.

    Settings ship with relative defaults (``secrets/jwt-private.pem``)
    so docker mount points stay short and obvious. We resolve against
    the current working directory at import time — uvicorn and pytest
    both run from the backend root so this lines up. Absolute paths
    pass through unchanged.
    """
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _auto_generate_keypair(private_path: Path, public_path: Path) -> None:
    """Invoke ``scripts/generate-jwt-keypair.py`` to produce a fresh pair.

    Only reachable when ``settings.ALLOW_AUTO_KEYGEN`` is True (dev /
    test). The subprocess approach keeps the keygen logic in one place
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
        # read-only mount, so the old advice ("set ALLOW_AUTO_KEYGEN=true")
        # dies with PermissionError one line later — a remedy that cannot
        # work is worse than no remedy, because the operator spends the
        # outage trying it. Point at the host-side provisioning step instead.
        raise JwtKeyLoadError(
            f"JWT {label} key not found at {path}. "
            "Provision it from the host, then restart this service: "
            "`bash infra/deployment/scripts/deploy-prod.sh` (its "
            "ensure_jwt_keypair step) or, on the intranet host, "
            "intranet-deploy.sh step [4b]; both write into ./secrets as root. "
            "Then run infra/deployment/scripts/fix-runtime-ownership.sh so "
            "the non-root runtime user can read the key. "
            "ALLOW_AUTO_KEYGEN=true only helps where the process can write "
            "the key directory itself (local dev / pytest) — it cannot work "
            "in the container."
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
            "The key was provisioned but the ownership alignment step was "
            "skipped: run infra/deployment/scripts/fix-runtime-ownership.sh "
            "on the host, then restart this service. That step leaves the "
            "key owned by whoever created it and only adds group read for "
            "the runtime user (mode 0640, group = the container's gid); it "
            "does not move ownership away from the deployer. "
            "Do not chmod the key world-readable as a workaround."
        ) from exc


@lru_cache(maxsize=1)
def _load_keys() -> tuple[bytes, bytes]:
    """Read PEM files lazily on first use.

    Lazy load (vs module-import time) lets tests monkeypatch
    ``settings.JWT_PRIVATE_KEY_PATH`` before the first sign/verify call.
    The ``lru_cache`` ensures we hit the disk exactly once per process.
    Tests that need to swap keys mid-run call ``_load_keys.cache_clear()``.
    """
    private_path = _resolve_key_path(settings.JWT_PRIVATE_KEY_PATH)
    public_path = _resolve_key_path(settings.JWT_PUBLIC_KEY_PATH)

    missing = not (private_path.exists() and public_path.exists())
    if missing and settings.ALLOW_AUTO_KEYGEN:
        logger.warning(
            "[security] JWT keypair missing — auto-generating at %s / %s "
            "(ALLOW_AUTO_KEYGEN=True). Do NOT use this code path in production.",
            private_path,
            public_path,
        )
        _auto_generate_keypair(private_path, public_path)

    private_pem = _load_pem(private_path, label="private")
    public_pem = _load_pem(public_path, label="public")
    return private_pem, public_pem


def get_private_key() -> bytes:
    """Return the RS256 signing key (PKCS#8 PEM bytes)."""
    private_pem, _ = _load_keys()
    return private_pem


def get_public_key() -> bytes:
    """Return the RS256 verification key (SPKI PEM bytes).

    Exposed so the JWKS endpoint can serialise the public modulus
    without re-reading from disk.
    """
    _, public_pem = _load_keys()
    return public_pem


def _public_key_for_kid(kid: str | None) -> bytes | None:
    """Resolve a JWT ``kid`` header value to the matching public key.

    Today we host exactly one active key (``settings.JWT_KID``). Future
    rotations (``anila-v2`` etc.) drop in here without touching the
    rest of the verify path. A missing or unknown ``kid`` returns None
    so the verifier can reject the token deterministically.
    """
    if not kid:
        return None
    if kid == settings.JWT_KID:
        return get_public_key()
    return None


# ── Token signing ─────────────────────────────────────────────────────────────

def _jwt_headers() -> dict:
    """Headers attached to every CSP-signed JWT.

    The ``kid`` lets downstream verifiers pick the right JWKS entry.
    ``typ`` follows RFC 7519 §5.1 so generic JWT tooling treats the
    payload correctly.
    """
    return {"kid": settings.JWT_KID, "typ": "JWT"}


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    to_encode.update({"exp": expire, "type": "access"})
    return jwt.encode(
        to_encode,
        get_private_key(),
        algorithm=ALGORITHM,
        headers=_jwt_headers(),
    )


def create_refresh_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(
        days=settings.REFRESH_TOKEN_EXPIRE_DAYS
    )
    to_encode.update({"exp": expire, "type": "refresh"})
    return jwt.encode(
        to_encode,
        get_private_key(),
        algorithm=ALGORITHM,
        headers=_jwt_headers(),
    )


# ── Token verification ────────────────────────────────────────────────────────

def decode_token(token: str) -> dict | None:
    """Verify ``token`` and return its claims, or None on any failure.

    Defence-in-depth:

    1. Reject the token outright if its header is malformed or its
       ``kid`` is missing/unknown — refusing to look up a key means the
       generic verify path never has a chance to mis-fire.
    2. Call ``jwt.decode`` with an explicit ``algorithms=["RS256"]``
       allowlist so ``alg=none`` and ``alg=HS256`` are rejected before
       any key material is touched (algorithm-confusion defence).
    3. Any ``JWTError`` (expired, bad signature, claim mismatch, …)
       returns None — callers raise the user-facing 401 themselves.
    """
    try:
        header = jwt.get_unverified_header(token)
    except JWTError:
        return None
    kid = header.get("kid")
    public_key = _public_key_for_kid(kid)
    if public_key is None:
        return None
    try:
        return jwt.decode(
            token,
            public_key,
            algorithms=[ALGORITHM],
        )
    except JWTError:
        return None


def verify_token(token: str) -> dict | None:
    """Alias for ``decode_token`` — kept so callers reading the name
    understand the intent without surprising them on rename. Both go
    through the same RS256 verify path."""
    return decode_token(token)


def get_kid() -> str:
    """Active signing key id — exposed for the JWKS endpoint."""
    return settings.JWT_KID


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
