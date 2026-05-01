"""Service token envelope helpers for ``agent_credentials`` /
``service_clients`` rows.

Sprint 8 X / Phase A — bootstrap-then-provision rollout.

Two storage shapes coexist on each row:

    service_token_envelope        TEXT       enc::v1::<base64(nonce|tag|ct)>
    service_token_lookup_hash     CHAR(64)   hex(sha256(plaintext))

The envelope is the **authoritative ciphertext**; the lookup hash is a
deterministic non-secret index so middleware can find the row without
scanning + decrypting every credential. SHA-256 over a 256-bit-entropy
plaintext is **not** a security boundary on its own (verify still uses
``hmac.compare_digest`` against the decrypted envelope) — it is purely a
B-tree key. Anyone with DB read access already has the master key and
can decrypt the envelope, so leaking the hash adds no information.

Format reuse:
    Same ``enc::v1::`` prefix and base64-urlsafe layout as
    ``auth_provider_secret.encode_oidc_client_secret``. Means a single
    audit / re-encrypt script can sweep both columns later.

Token plaintext format:
    Bootstrap tokens   →  ``bsk-<43-chars>``  (admin-issued, single-use)
    Service tokens     →  ``csk-<43-chars>``  (long-lived, rotated)

The 43 chars come from ``secrets.token_urlsafe(32)`` which produces
~32 random bytes ≈ 43 base64url characters (256 bits of entropy).

Why ``bsk-`` / ``csk-`` prefixes:
    * Make TruffleHog / GitGuardian / similar secret scanners catchable
      out of the box. Generic random strings get missed; prefixed
      tokens do not.
    * Let humans tell which kind of token they're holding without
      having to ask the issuer.
"""
from __future__ import annotations

import base64
import hashlib
import secrets

from anila_core.security.credential_crypto import (
    decrypt_credential,
    encrypt_credential,
)


ENVELOPE_PREFIX = "enc::v1::"

BOOTSTRAP_TOKEN_PREFIX = "bsk-"
SERVICE_TOKEN_PREFIX = "csk-"

# 32 random bytes → 43 base64url chars. 256 bits of entropy. The
# ``secrets`` module is the python equivalent of /dev/urandom — never
# downgrade this to ``random``.
_TOKEN_ENTROPY_BYTES = 32


def generate_bootstrap_token() -> str:
    """Mint a fresh ``bsk-`` token for admin-driven agent bootstrap.

    Single-use; the issuer stores its sha256 hash + expiry on the
    ``agents`` row and consumes it on first ``POST /bootstrap``.
    """
    return f"{BOOTSTRAP_TOKEN_PREFIX}{secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)}"


def generate_service_token() -> str:
    """Mint a fresh ``csk-`` token for an agent_credentials / service_clients row.

    Long-lived (rotation-managed). Always written via
    ``encode_service_token_envelope`` for at-rest encryption.
    """
    return f"{SERVICE_TOKEN_PREFIX}{secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)}"


def encode_service_token_envelope(plaintext: str) -> str:
    """Wrap a plaintext service token into the ``enc::v1::...`` envelope.

    AES-256-GCM via the shared ``credential_crypto`` helper. Output is
    a single TEXT-safe string suitable for direct column storage.
    """
    if not plaintext:
        raise ValueError("無法將空字串編碼為 service token envelope")
    ct, nonce, tag = encrypt_credential(plaintext)
    blob = base64.urlsafe_b64encode(nonce + tag + ct).decode("ascii")
    return f"{ENVELOPE_PREFIX}{blob}"


def decode_service_token_envelope(stored: str | None) -> str | None:
    """Inverse of ``encode_service_token_envelope``.

    Returns ``None`` for ``None`` / empty input so callers can pass the
    column value straight through. Raises ``ValueError`` on a corrupted
    envelope; raises ``cryptography.exceptions.InvalidTag`` on a wrong
    master key.
    """
    if not stored:
        return None
    if not stored.startswith(ENVELOPE_PREFIX):
        raise ValueError(
            "service_token_envelope 缺少 enc::v1:: 前綴，疑似資料損毀或未加密 row"
        )
    blob = stored[len(ENVELOPE_PREFIX):]
    raw = base64.urlsafe_b64decode(blob.encode("ascii"))
    if len(raw) < 12 + 16:
        raise ValueError("service_token_envelope 過短，疑似已損毀")
    nonce, tag, ct = raw[:12], raw[12:28], raw[28:]
    return decrypt_credential(ct, nonce, tag)


def compute_lookup_hash(plaintext: str) -> str:
    """Deterministic 64-char hex SHA-256 of the plaintext token.

    Used as the indexed lookup key on ``agent_credentials`` /
    ``service_clients``. **Not** a security check — verification must
    still constant-time-compare against the decrypted envelope.
    """
    if not plaintext:
        raise ValueError("無法對空字串計算 lookup hash")
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def hash_bootstrap_token(plaintext: str) -> str:
    """SHA-256 of a ``bsk-`` token for storage on ``agents.bootstrap_token_hash``.

    Bootstrap tokens are stored hash-only (single-use, no need for
    plaintext recovery). Same algorithm as ``compute_lookup_hash`` but
    semantically different — keep them as separate functions so
    grep / refactor distinguishes the two storage paths.
    """
    return compute_lookup_hash(plaintext)


__all__ = [
    "BOOTSTRAP_TOKEN_PREFIX",
    "ENVELOPE_PREFIX",
    "SERVICE_TOKEN_PREFIX",
    "compute_lookup_hash",
    "decode_service_token_envelope",
    "encode_service_token_envelope",
    "generate_bootstrap_token",
    "generate_service_token",
    "hash_bootstrap_token",
]
