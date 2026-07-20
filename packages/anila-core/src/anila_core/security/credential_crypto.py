"""Compatibility facade for :mod:`anila_security.credential_crypto`.

The implementation moved to the standalone ``anila-security`` package in
Gate 1 F4.  Keep these re-exports so existing deployments and third-party
callers do not need an atomic import migration.
"""

from anila_security.credential_crypto import (
    decrypt_credential,
    encrypt_credential,
    legacy_fallback_count,
    reset_legacy_fallback_count,
)

__all__ = [
    "decrypt_credential",
    "encrypt_credential",
    "legacy_fallback_count",
    "reset_legacy_fallback_count",
]
