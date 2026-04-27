"""AES-256-GCM helper for encrypted credential columns.

Sprint 5 / Chunk X: copied (verbatim) from
``myCSPPlatform/backend/app/services/credential_crypto.py`` to anila-core
so both the CSP backend (encrypt at create-time) and the
ingestion-worker (decrypt at judge-call-time) can share one
implementation without one importing the other's package.

Sprint 6 X / A2: PBKDF2 iters bumped from 100k → 600k (OWASP 2024
guideline for SHA-256). Existing rows encrypted with the legacy 100k
key would fail to decrypt under the new key, so ``decrypt_credential``
now tries the new key first and transparently falls back to the legacy
key on AES-GCM auth failure. ``encrypt_credential`` always writes with
the new key, so re-saving any decrypted row migrates it to v2 in place.

Master key derivation:

    derived_key = PBKDF2(
        password = SECRET_KEY (env, accepts SECRET_KEY or CSP_SECRET_KEY),
        salt     = "agent_llm_credentials_v1",  # legacy name preserved for
                                                  back-compat with v0.7
                                                  encrypted rows
        iters    = 600_000 (writes) / 100_000 (legacy decrypt fallback)
        hash     = SHA-256,
        out_len  = 32 bytes (256 bits),
    )

The fixed salt is intentional — same master key on every process
boot. ``SECRET_KEY`` (or ``CSP_SECRET_KEY``, kept as fallback) is the
single source of secrecy; rotating it invalidates every stored
credential. That's the desired kill-switch property.

To rotate the master, or to retire the legacy 100k fallback once every
row has been re-encrypted, ops can use the helper script in
``scripts/reencrypt-credentials.py`` (Sprint 6 X / A2 follow-up).
"""

from __future__ import annotations

import logging
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


logger = logging.getLogger(__name__)


# Salt name preserved from when the column was on
# ``agent_llm_credentials`` (Sprint 3 / Chunk L). Sprint 4 / migration
# 0019 renamed the table to ``user_llm_credentials`` but we keep the
# salt to avoid invalidating every existing encrypted row.
_DERIVATION_SALT = b"agent_llm_credentials_v1"
# Sprint 6 X / A2: 寫一律走 600k；讀失敗 fallback 100k 並由呼叫端視需要
# 觸發 re-encrypt（見 ``scripts/reencrypt-credentials.py``）。
_DERIVATION_ITERS = 600_000
_DERIVATION_ITERS_LEGACY = 100_000
_NONCE_BYTES = 12  # GCM standard

# Sprint 5 / Chunk X security review (H2): ops sometimes ship docker
# compose without overriding the dev default, encrypting all rows with
# a publicly-known string. We refuse to start unless either the secret
# is actually customised OR the operator opts in via env.
_KNOWN_DEV_SECRETS = frozenset({
    "dev-secret-key-change-in-prod",
    "change-me",
    "change_me",
    "secret",
    "",
})

# 統計指標：每次 legacy fallback 命中時 +1，由 ingestion-worker /
# CSP 在啟動 / 排程時可讀此 counter 判斷是否還剩下 v1 row。重入安全：
# Python int 賦值是原子的；不需要 lock。
_legacy_fallback_count = 0


def _derive_key(*, iters: int = _DERIVATION_ITERS) -> bytes:
    secret = os.environ.get("SECRET_KEY") or os.environ.get("CSP_SECRET_KEY")
    if not secret:
        raise RuntimeError(
            "SECRET_KEY (or CSP_SECRET_KEY) env var must be set for "
            "credential encryption."
        )
    if (
        secret.strip().lower() in _KNOWN_DEV_SECRETS
        and os.environ.get("ANILA_ALLOW_DEV_SECRET") != "1"
    ):
        raise RuntimeError(
            "SECRET_KEY is the dev default. All user_llm_credentials would "
            "be encrypted with a publicly-known key. Set CSP_SECRET_KEY "
            "(or SECRET_KEY) to a real production value, or — only for "
            "intentional dev work — export ANILA_ALLOW_DEV_SECRET=1."
        )
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_DERIVATION_SALT,
        iterations=iters,
    )
    return kdf.derive(secret.encode("utf-8"))


def encrypt_credential(plaintext: str) -> tuple[bytes, bytes, bytes]:
    """Return ``(ciphertext, nonce, tag)`` triple for one credential.

    Always written with the current PBKDF2 iters (600k). Re-encrypting a
    legacy row with this function silently upgrades it to v2.
    """
    key = _derive_key()
    nonce = os.urandom(_NONCE_BYTES)
    aead = AESGCM(key)
    ct_with_tag = aead.encrypt(nonce, plaintext.encode("utf-8"), None)
    ciphertext, tag = ct_with_tag[:-16], ct_with_tag[-16:]
    return ciphertext, nonce, tag


def decrypt_credential(ciphertext: bytes, nonce: bytes, tag: bytes) -> str:
    """Reverse of ``encrypt_credential``. Raises on tampering / wrong key.

    Tries the current key (600k iters) first; on ``InvalidTag`` falls
    back to the legacy key (100k iters) so rows encrypted before
    Sprint 6 X / A2 still decrypt. Each fallback hit is logged at
    INFO level + counted in ``legacy_fallback_count`` so ops can tell
    when the migration is complete.
    """
    global _legacy_fallback_count
    blob = ciphertext + tag

    # 試新 key（一律先試 600k；新 row 直接成功）。
    try:
        aead = AESGCM(_derive_key())
        return aead.decrypt(nonce, blob, None).decode("utf-8")
    except InvalidTag:
        pass

    # InvalidTag → 試 legacy 100k key。若連 legacy 也失敗就讓 InvalidTag
    # 一路往上拋（呼叫端能藉此區分「真的被竄改」vs「key 不對」）。
    aead_legacy = AESGCM(_derive_key(iters=_DERIVATION_ITERS_LEGACY))
    plaintext = aead_legacy.decrypt(nonce, blob, None).decode("utf-8")
    _legacy_fallback_count += 1
    logger.info(
        "credential_crypto: legacy (100k) decrypt fallback used; consider "
        "running scripts/reencrypt-credentials.py to migrate (count=%d)",
        _legacy_fallback_count,
    )
    return plaintext


def legacy_fallback_count() -> int:
    """Cumulative count of v1→v2 fallback decrypts since process boot.

    Used by the re-encrypt migration script to confirm progress, and by
    ops dashboards to tell when v1 rows have all been retired.
    """
    return _legacy_fallback_count


def reset_legacy_fallback_count() -> None:
    """Test helper — resets the counter so per-test assertions stay clean."""
    global _legacy_fallback_count
    _legacy_fallback_count = 0
