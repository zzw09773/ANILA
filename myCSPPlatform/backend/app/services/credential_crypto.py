"""AES-256-GCM helper for ``agent_llm_credentials.api_key_*`` columns.

Why GCM rather than CBC + HMAC: GCM gives us authenticated encryption
in one primitive; the 16-byte auth tag detects ciphertext tampering
out-of-the-box. Per-row 12-byte nonce eliminates the IV-reuse hazard.

Master key derivation:

    derived_key = PBKDF2(
        password = CSP_SECRET_KEY (env),
        salt     = "agent_llm_credentials_v1",
        iters    = 100_000,
        hash     = SHA-256,
        out_len  = 32 bytes (256 bits),
    )

The fixed salt is intentional — we want the same master key on every
process boot. ``CSP_SECRET_KEY`` is the single source of secrecy;
losing or rotating it invalidates every stored credential. That's
the desired property: rotating CSP_SECRET_KEY is the kill switch.

To rotate the master without losing data, the operator must:
1. Decrypt every row with the old key.
2. Re-encrypt with the new key.
3. UPDATE the rows.
The migration tool for that is a Sprint 4 concern; for now, rotation
== re-issue all credentials.
"""

from __future__ import annotations

import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes


_DERIVATION_SALT = b"agent_llm_credentials_v1"
_DERIVATION_ITERS = 100_000
_NONCE_BYTES = 12  # GCM standard


def _derive_key() -> bytes:
    secret = os.environ.get("SECRET_KEY") or os.environ.get("CSP_SECRET_KEY")
    if not secret:
        raise RuntimeError(
            "CSP_SECRET_KEY (or SECRET_KEY) env var must be set for "
            "agent_llm_credentials encryption."
        )
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_DERIVATION_SALT,
        iterations=_DERIVATION_ITERS,
    )
    return kdf.derive(secret.encode("utf-8"))


def encrypt_credential(plaintext: str) -> tuple[bytes, bytes, bytes]:
    """Return ``(ciphertext, nonce, tag)`` triple for one credential.

    The cryptography lib's AESGCM bundles ciphertext + tag into one
    bytes object (last 16 bytes are the tag). We split for storage
    so the tag column is auditable on its own — comparing tag bytes
    across rows is the cheapest tampering-detection signal.
    """
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
