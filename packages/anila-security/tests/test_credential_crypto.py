from __future__ import annotations

import os

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from anila_security import credential_crypto


@pytest.fixture(autouse=True)
def _credential_env(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "unit-test-master-key-with-enough-entropy")
    monkeypatch.delenv("CSP_SECRET_KEY", raising=False)
    monkeypatch.delenv("ANILA_ALLOW_DEV_SECRET", raising=False)
    credential_crypto.reset_legacy_fallback_count()
    yield
    credential_crypto.reset_legacy_fallback_count()


def test_round_trip_preserves_unicode_and_binary_shape():
    ciphertext, nonce, tag = credential_crypto.encrypt_credential("密碼-abc-123")

    assert isinstance(ciphertext, bytes)
    assert len(nonce) == 12
    assert len(tag) == 16
    assert credential_crypto.decrypt_credential(ciphertext, nonce, tag) == "密碼-abc-123"
    assert credential_crypto.legacy_fallback_count() == 0


def test_tampering_fails_closed():
    ciphertext, nonce, tag = credential_crypto.encrypt_credential("secret")
    tampered = bytes([ciphertext[0] ^ 1]) + ciphertext[1:]

    with pytest.raises(InvalidTag):
        credential_crypto.decrypt_credential(tampered, nonce, tag)


def test_known_development_secret_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "dev-secret-key-change-in-prod")

    with pytest.raises(RuntimeError, match="dev default"):
        credential_crypto.encrypt_credential("must-not-encrypt")

    monkeypatch.setenv("ANILA_ALLOW_DEV_SECRET", "1")
    ciphertext, nonce, tag = credential_crypto.encrypt_credential("allowed-dev-only")
    assert credential_crypto.decrypt_credential(ciphertext, nonce, tag) == "allowed-dev-only"


def test_legacy_100k_ciphertext_is_read_and_counted():
    legacy_key = credential_crypto._derive_key(  # noqa: SLF001 - migration contract
        iters=credential_crypto._DERIVATION_ITERS_LEGACY  # noqa: SLF001
    )
    nonce = os.urandom(12)
    blob = AESGCM(legacy_key).encrypt(nonce, b"legacy-row", None)

    assert credential_crypto.decrypt_credential(blob[:-16], nonce, blob[-16:]) == "legacy-row"
    assert credential_crypto.legacy_fallback_count() == 1
