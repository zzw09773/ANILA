"""Encrypted OIDC client_secret storage helpers.

Sprint 5 X security review (H1): ``oidc_client_secret`` used to be stored
plaintext in ``auth_providers``. This module wraps the existing
``anila_core.security.credential_crypto`` AES-256-GCM helper so the secret
lives encrypted at rest, and is only decrypted just-in-time when CSP
exchanges an OIDC ``code`` for tokens.

Storage shape: we reuse the existing ``oidc_client_secret`` column but
serialise the ciphertext + nonce + tag as a single base64 envelope so we
don't need an Alembic migration to add new columns. New rows write the
envelope; legacy plaintext rows are detected by the ``ENVELOPE_PREFIX``
sentinel and migrated lazily on next save.
"""
from __future__ import annotations

import base64

from anila_core.security.credential_crypto import (
    decrypt_credential,
    encrypt_credential,
)

# 標識「此欄位是加密 envelope」而非舊 plaintext。任何不以這個前綴開頭的
# 值都被視為「尚未遷移」的舊 plaintext，讀取時會被當成 plaintext 回傳，
# 下一次寫入時自動轉為 envelope。
ENVELOPE_PREFIX = "enc::v1::"


def encode_oidc_client_secret(plaintext: str | None) -> str | None:
    """Return the envelope to persist into ``auth_providers.oidc_client_secret``.

    ``None`` / empty string passes through so admins can clear the secret.
    """
    if not plaintext:
        return None
    ct, nonce, tag = encrypt_credential(plaintext)
    blob = base64.urlsafe_b64encode(nonce + tag + ct).decode("ascii")
    return f"{ENVELOPE_PREFIX}{blob}"


def decode_oidc_client_secret(stored: str | None) -> str | None:
    """Inverse of ``encode_oidc_client_secret``. Plaintext rows pass through."""
    if not stored:
        return None
    if not stored.startswith(ENVELOPE_PREFIX):
        # 舊 row：尚未遷移到 envelope，直接回傳 plaintext。寫入路徑會把它
        # 包成 envelope。
        return stored
    blob = stored[len(ENVELOPE_PREFIX):]
    raw = base64.urlsafe_b64decode(blob.encode("ascii"))
    if len(raw) < 12 + 16:
        raise ValueError("auth_provider envelope 太短，疑似已損毀")
    nonce, tag, ct = raw[:12], raw[12:28], raw[28:]
    return decrypt_credential(ct, nonce, tag)


def load_oidc_client_secret(provider) -> str | None:
    """Read + decrypt the OIDC client_secret from a provider row."""
    return decode_oidc_client_secret(provider.oidc_client_secret)
