from __future__ import annotations

import pytest

import anila_core.security as legacy
import anila_core.security.credential_crypto as legacy_crypto
import anila_core.security.url_guard as legacy_guard
import anila_security as canonical
import anila_security.credential_crypto as canonical_crypto
import anila_security.url_guard as canonical_guard


def test_legacy_public_api_reexports_canonical_objects():
    assert legacy.encrypt_credential is canonical.encrypt_credential
    assert legacy.decrypt_credential is canonical.decrypt_credential
    assert legacy.UnsafeEndpointError is canonical.UnsafeEndpointError
    assert legacy.validate_outbound_url is canonical.validate_outbound_url
    assert legacy.register_trusted_host_provider is canonical.register_trusted_host_provider
    assert legacy.clear_trusted_host_providers is canonical.clear_trusted_host_providers
    assert legacy_crypto.encrypt_credential is canonical_crypto.encrypt_credential
    assert legacy_guard.validate_outbound_url is canonical_guard.validate_outbound_url


def test_legacy_and_canonical_crypto_paths_interoperate(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "compatibility-test-master-key")
    ciphertext, nonce, tag = legacy.encrypt_credential("same-wire-format")
    assert canonical.decrypt_credential(ciphertext, nonce, tag) == "same-wire-format"


def test_trusted_host_registry_is_not_duplicated(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    canonical.clear_trusted_host_providers()
    try:
        legacy.register_trusted_host_provider(lambda: {"shared-provider"})
        canonical.validate_outbound_url("http://shared-provider:8000/v1")
    finally:
        canonical.clear_trusted_host_providers()


def test_legacy_exception_catches_canonical_failure(monkeypatch):
    monkeypatch.delenv("ANILA_ALLOW_HTTP_ENDPOINT", raising=False)
    with pytest.raises(legacy.UnsafeEndpointError):
        canonical.validate_outbound_url("http://example.com/v1")
