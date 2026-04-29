"""AES-256-GCM at-rest encryption for ANILA Functions admin Valves.

Why this exists
---------------

Spec §7.3 mandates that admin Valves never appear in plaintext in any
persistent storage (DB column or audit log). This helper encrypts the
``values_json`` blob with AES-256-GCM using a single key loaded from the
``ANILA_FUNCTIONS_VALVES_KEY`` environment variable (base64-encoded 32
bytes). The accompanying nonce is generated per-encrypt and stored
alongside the ciphertext.

Key rotation in v1 is manual: bump ``CURRENT_KEY_VERSION``, run the
migration script (TBD in Sprint 3 ops tooling) that re-encrypts every
``action_function_valves`` row with the new key, then deploy. v2 will
add an automatic rotation routine.

This module deliberately exposes a tiny surface (``encrypt_valves`` /
``decrypt_valves``) — anything fancier (HSM-backed keys, KMS plumbing,
per-tenant subkeys) belongs in v2 once the operational story is
clearer.
"""

from __future__ import annotations

import base64
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


CURRENT_KEY_VERSION = 1
NONCE_LEN = 12  # AES-GCM standard nonce size in bytes


class InvalidKeyError(Exception):
    """Raised on missing key, wrong key length, or decrypt-time tag mismatch."""


def _load_key() -> bytes:
    raw = os.environ.get("ANILA_FUNCTIONS_VALVES_KEY", "").strip()
    if not raw:
        raise InvalidKeyError("ANILA_FUNCTIONS_VALVES_KEY not set")
    try:
        key = base64.b64decode(raw)
    except Exception as exc:
        raise InvalidKeyError(f"key not valid base64: {exc}") from exc
    if len(key) != 32:
        raise InvalidKeyError(
            f"AES-256-GCM key must decode to 32 bytes, got {len(key)}"
        )
    return key


def encrypt_valves(values: dict) -> tuple[bytes, bytes, int]:
    """Serialize-and-encrypt a Valves payload.

    Returns ``(ciphertext, nonce, key_version)`` for storage in the
    ``action_function_valves`` row. JSON is sorted-key-canonicalised so
    the same input produces the same plaintext (helps idempotent
    redaction matching during audit).
    """
    key = _load_key()
    nonce = os.urandom(NONCE_LEN)
    aesgcm = AESGCM(key)
    plaintext = json.dumps(values, sort_keys=True).encode("utf-8")
    ciphertext = aesgcm.encrypt(nonce, plaintext, associated_data=None)
    return ciphertext, nonce, CURRENT_KEY_VERSION


def decrypt_valves(ciphertext: bytes, nonce: bytes, key_version: int) -> dict:
    """Decrypt a stored Valves payload back to its dict form.

    Mismatch on ``key_version`` (e.g. row was written with an older key
    that's been rotated out) is surfaced as :class:`InvalidKeyError` so
    callers can decide whether to attempt fallback decryption with the
    archived key. v1 has only one key so this just guards against
    forgotten migrations.
    """
    if key_version != CURRENT_KEY_VERSION:
        raise InvalidKeyError(
            f"row key_version={key_version}, current={CURRENT_KEY_VERSION}; "
            "rotate via migration before decrypting"
        )
    key = _load_key()
    aesgcm = AESGCM(key)
    try:
        plaintext = aesgcm.decrypt(nonce, ciphertext, associated_data=None)
    except InvalidTag as exc:
        raise InvalidKeyError(
            "AES-GCM decrypt failed (key wrong or ciphertext tampered)"
        ) from exc
    return json.loads(plaintext.decode("utf-8"))
