"""AES-256-GCM helper for encrypted credential columns.

Sprint 5 / Chunk X: copied (verbatim) from
``myCSPPlatform/backend/app/services/credential_crypto.py`` to anila-core
so both the CSP backend (encrypt at create-time) and the
ingestion-worker (decrypt at judge-call-time) can share one
implementation without one importing the other's package.

Master key derivation:

    derived_key = PBKDF2(
        password = SECRET_KEY (env, accepts SECRET_KEY or CSP_SECRET_KEY),
        salt     = "agent_llm_credentials_v1",  # legacy name preserved for
                                                  back-compat with v0.7
                                                  encrypted rows
        iters    = 100_000,
        hash     = SHA-256,
        out_len  = 32 bytes (256 bits),
    )

The fixed salt is intentional — same master key on every process
boot. ``SECRET_KEY`` (or ``CSP_SECRET_KEY``, kept as fallback) is the
single source of secrecy; rotating it invalidates every stored
credential. That's the desired kill-switch property.

To rotate the master without losing data, ops must:
1. Decrypt every row with the old key.
2. Re-encrypt with the new key.
3. UPDATE the rows.
The migration tool for that is Sprint 6 territory; for now,
rotation == re-issue all credentials.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


# Salt name preserved from when the column was on
# ``agent_llm_credentials`` (Sprint 3 / Chunk L). Sprint 4 / migration
# 0019 renamed the table to ``user_llm_credentials`` but we keep the
# salt to avoid invalidating every existing encrypted row.
_DERIVATION_SALT = b"agent_llm_credentials_v1"
_DERIVATION_ITERS = 100_000
_NONCE_BYTES = 12  # GCM standard


def _derive_key() -> bytes:
    secret = os.environ.get("SECRET_KEY") or os.environ.get("CSP_SECRET_KEY")
    if not secret:
        raise RuntimeError(
            "SECRET_KEY (or CSP_SECRET_KEY) env var must be set for "
            "credential encryption."
        )
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_DERIVATION_SALT,
        iterations=_DERIVATION_ITERS,
    )
    return kdf.derive(secret.encode("utf-8"))


def encrypt_credential(plaintext: str) -> tuple[bytes, bytes, bytes]:
    """Return ``(ciphertext, nonce, tag)`` triple for one credential."""
    key = _derive_key()
    nonce = os.urandom(_NONCE_BYTES)
    aead = AESGCM(key)
    ct_with_tag = aead.encrypt(nonce, plaintext.encode("utf-8"), None)
    ciphertext, tag = ct_with_tag[:-16], ct_with_tag[-16:]
    return ciphertext, nonce, tag


def decrypt_credential(ciphertext: bytes, nonce: bytes, tag: bytes) -> str:
    """Reverse of ``encrypt_credential``. Raises on tampering / wrong key."""
    key = _derive_key()
    aead = AESGCM(key)
    pt = aead.decrypt(nonce, ciphertext + tag, None)
    return pt.decode("utf-8")
