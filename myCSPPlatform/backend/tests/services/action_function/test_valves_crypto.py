"""Tests for ANILA Functions valves AES-256-GCM helper.

Run inside the CSP container (or a venv with cryptography installed):

    pytest tests/services/action_function/test_valves_crypto.py -v
"""

from __future__ import annotations

import base64
import os

import pytest

from app.services.action_function.valves_crypto import (
    InvalidKeyError,
    decrypt_valves,
    encrypt_valves,
)


@pytest.fixture
def key_b64() -> str:
    # 32 random bytes, base64-encoded — anchors the tests in a
    # deterministic key without ever touching the real production key.
    return base64.b64encode(os.urandom(32)).decode("ascii")


def test_encrypt_decrypt_round_trip(key_b64: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANILA_FUNCTIONS_VALVES_KEY", key_b64)
    payload = {
        "api_endpoint": "https://lint.internal",
        "threshold": 5,
        "tags": ["a", "b"],
    }
    blob, nonce, version = encrypt_valves(payload)
    assert version == 1
    assert isinstance(blob, bytes) and len(blob) > 0
    assert isinstance(nonce, bytes) and len(nonce) == 12
    assert decrypt_valves(blob, nonce, version) == payload


def test_decrypt_with_wrong_key_raises(key_b64: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANILA_FUNCTIONS_VALVES_KEY", key_b64)
    blob, nonce, version = encrypt_valves({"x": 1})
    other = base64.b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv("ANILA_FUNCTIONS_VALVES_KEY", other)
    with pytest.raises(InvalidKeyError, match="decrypt failed"):
        decrypt_valves(blob, nonce, version)


def test_missing_key_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANILA_FUNCTIONS_VALVES_KEY", raising=False)
    with pytest.raises(InvalidKeyError, match="not set"):
        encrypt_valves({"x": 1})


def test_invalid_base64_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANILA_FUNCTIONS_VALVES_KEY", "not-valid-base64!!!")
    with pytest.raises(InvalidKeyError, match="base64"):
        encrypt_valves({"x": 1})


def test_wrong_length_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    short = base64.b64encode(b"too-short").decode("ascii")  # 9 bytes, not 32
    monkeypatch.setenv("ANILA_FUNCTIONS_VALVES_KEY", short)
    with pytest.raises(InvalidKeyError, match="32 bytes"):
        encrypt_valves({"x": 1})


def test_unknown_key_version_raises(key_b64: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A row written with a future key_version should not silently decrypt
    with the current key — surface the mismatch so ops knows rotation is
    incomplete.
    """
    monkeypatch.setenv("ANILA_FUNCTIONS_VALVES_KEY", key_b64)
    blob, nonce, _version = encrypt_valves({"x": 1})
    with pytest.raises(InvalidKeyError, match="key_version=99"):
        decrypt_valves(blob, nonce, key_version=99)


def test_nonce_is_unique_per_encrypt(key_b64: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANILA_FUNCTIONS_VALVES_KEY", key_b64)
    nonces = {encrypt_valves({"x": i})[1] for i in range(50)}
    # 50 random 12-byte nonces colliding is astronomically unlikely
    assert len(nonces) == 50
