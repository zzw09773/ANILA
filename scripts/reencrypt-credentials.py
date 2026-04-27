"""One-shot re-encryption tool for credential rows under PBKDF2 v1 → v2.

Sprint 6 X / A2 follow-up. After the codebase moved from PBKDF2 100k to
600k iterations, ``decrypt_credential`` transparently falls back to the
100k key when v2 fails — so existing rows keep working but stay v1
encrypted at rest. This script reads every encrypted row, decrypts it
(possibly via legacy fallback), and re-encrypts under the new key.

Tables touched:

- ``user_llm_credentials``: ``api_key_encrypted`` / ``_nonce`` / ``_tag``
- ``auth_providers.oidc_client_secret``: ``enc::v1::`` envelope (the
  envelope already encodes nonce + tag; we just round-trip
  ``decode_oidc_client_secret`` → ``encode_oidc_client_secret``).

Usage (inside the CSP container so SECRET_KEY is in env):

    docker exec anila-platform-csp-1 \\
        python -m scripts.reencrypt_credentials --apply

Run with ``--dry-run`` (default) to see the count first.

Idempotent: a row that's already v2 will round-trip without raising
``legacy_fallback_count``; the script reports how many rows were truly
migrated (counter delta) versus left untouched.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

# Ensure the CSP backend package is importable when run as a one-off
# (matches scripts/init_db.py's path bootstrap).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "myCSPPlatform", "backend"))

from anila_core.security.credential_crypto import (  # noqa: E402
    encrypt_credential,
    decrypt_credential,
    legacy_fallback_count,
    reset_legacy_fallback_count,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("reencrypt-credentials")


def _reencrypt_user_llm_credentials(db, *, apply: bool) -> tuple[int, int]:
    """Round-trip every ``user_llm_credentials`` row.

    Returns ``(rows_total, rows_migrated)``. ``rows_migrated`` reflects
    how many rows actually went through the legacy fallback path — i.e.
    how many were stored under v1 before this run.
    """
    from app.models.ingestion import UserLlmCredential

    before = legacy_fallback_count()
    total = migrated = 0
    for row in db.query(UserLlmCredential).order_by(UserLlmCredential.id).all():
        total += 1
        plaintext = decrypt_credential(
            bytes(row.api_key_encrypted),
            bytes(row.api_key_nonce),
            bytes(row.api_key_tag),
        )
        new_ct, new_nonce, new_tag = encrypt_credential(plaintext)
        if not apply:
            continue
        row.api_key_encrypted = new_ct
        row.api_key_nonce = new_nonce
        row.api_key_tag = new_tag
    if apply:
        db.commit()
    migrated = legacy_fallback_count() - before
    logger.info(
        "user_llm_credentials: %d rows scanned, %d migrated from v1 → v2",
        total, migrated,
    )
    return total, migrated


def _reencrypt_auth_providers(db, *, apply: bool) -> tuple[int, int]:
    """Round-trip every ``auth_providers.oidc_client_secret`` envelope."""
    from app.models.auth_provider import AuthProvider
    from app.services.auth_provider_secret import (
        decode_oidc_client_secret,
        encode_oidc_client_secret,
    )

    before = legacy_fallback_count()
    total = migrated = 0
    rows = (
        db.query(AuthProvider)
        .filter(AuthProvider.oidc_client_secret.isnot(None))
        .order_by(AuthProvider.id)
        .all()
    )
    for row in rows:
        total += 1
        plaintext = decode_oidc_client_secret(row.oidc_client_secret)
        if plaintext is None:
            continue
        if not apply:
            # 仍要走 decrypt 以累積 legacy_fallback_count，但不寫 DB。
            continue
        row.oidc_client_secret = encode_oidc_client_secret(plaintext)
    if apply:
        db.commit()
    migrated = legacy_fallback_count() - before
    logger.info(
        "auth_providers: %d rows scanned, %d migrated from v1 → v2",
        total, migrated,
    )
    return total, migrated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the re-encrypted rows. Without this flag the "
        "script only counts how many rows are v1.",
    )
    args = parser.parse_args()

    from app.database import SessionLocal

    reset_legacy_fallback_count()
    db = SessionLocal()
    try:
        u_total, u_migrated = _reencrypt_user_llm_credentials(db, apply=args.apply)
        a_total, a_migrated = _reencrypt_auth_providers(db, apply=args.apply)
    finally:
        db.close()

    mode = "applied" if args.apply else "dry-run"
    logger.info(
        "[%s] total scanned=%d, v1→v2 migrated=%d",
        mode, u_total + a_total, u_migrated + a_migrated,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
