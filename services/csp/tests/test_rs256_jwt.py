"""Sprint 9 / anila-studio extraction: RS256 JWT signing & verification.

These tests pin the post-cutover contract:

* ``ALGORITHM`` is RS256, not HS256.
* Tokens sign + verify round-trip with the configured keypair.
* Expired tokens are rejected.
* Tampered tokens (wrong signature) are rejected.
* Tokens whose ``kid`` doesn't match ``settings.JWT_KID`` are rejected.
* Tokens without a ``kid`` are rejected — defence against the classic
  algorithm-confusion attack where a malicious party crafts an
  ``alg=none`` or ``alg=HS256`` token to bypass verification.

The fixture writes a fresh keypair per test session into a tmp dir and
points the security module at it via monkeypatch + cache_clear, so
tests run independently of the repo-checked-in ``secrets/`` artefacts.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jose import jwt

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def rs256_keys(tmp_path: Path, monkeypatch) -> tuple[bytes, bytes]:
    """Provision a fresh RSA-2048 keypair the security module loads.

    Yields ``(private_pem, public_pem)`` so tests can directly verify
    signatures they crafted themselves.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    priv_path = tmp_path / "jwt-private.pem"
    pub_path = tmp_path / "jwt-public.pem"
    priv_path.write_bytes(private_pem)
    pub_path.write_bytes(public_pem)

    from app.config import settings
    from app.utils import security as sec_module

    monkeypatch.setattr(settings, "JWT_PRIVATE_KEY_PATH", str(priv_path))
    monkeypatch.setattr(settings, "JWT_PUBLIC_KEY_PATH", str(pub_path))
    sec_module._load_keys.cache_clear()
    yield private_pem, public_pem
    sec_module._load_keys.cache_clear()


# ── Algorithm pin ─────────────────────────────────────────────────────────────

def test_algorithm_is_rs256():
    """Hard pin so an accidental revert to HS256 fails the suite."""
    from app.utils.security import ALGORITHM
    assert ALGORITHM == "RS256"


# ── Round trip ────────────────────────────────────────────────────────────────

def test_create_and_decode_access_token_round_trip(rs256_keys):
    from app.utils.security import create_access_token, decode_token

    token = create_access_token({"sub": "7", "username": "alice", "role": "user"})

    payload = decode_token(token)
    assert payload is not None
    assert payload["sub"] == "7"
    assert payload["username"] == "alice"
    assert payload["type"] == "access"
    assert "exp" in payload


def test_create_and_decode_refresh_token_round_trip(rs256_keys):
    from app.utils.security import create_refresh_token, decode_token

    token = create_refresh_token({"sub": "7", "username": "alice"})

    payload = decode_token(token)
    assert payload is not None
    assert payload["type"] == "refresh"


def test_verify_token_alias_matches_decode_token(rs256_keys):
    """``verify_token`` exists for naming clarity; both paths must agree."""
    from app.utils.security import create_access_token, decode_token, verify_token

    token = create_access_token({"sub": "1"})
    assert decode_token(token) == verify_token(token)


def test_token_header_contains_kid_and_rs256(rs256_keys):
    """Downstream verifiers (anila-studio) rely on the ``kid`` header
    to pick the right public key from JWKS — pin both."""
    from app.config import settings
    from app.utils.security import create_access_token

    token = create_access_token({"sub": "1"})
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "RS256"
    assert header["kid"] == settings.JWT_KID


# ── Failure cases ─────────────────────────────────────────────────────────────

def test_expired_token_rejected(rs256_keys, monkeypatch):
    """A token whose ``exp`` is in the past must verify as None."""
    from app.utils.security import create_access_token, decode_token
    from app.config import settings

    # Bypass the freezer by signing with a deliberately-stale exp.
    monkeypatch.setattr(settings, "ACCESS_TOKEN_EXPIRE_MINUTES", -1)
    token = create_access_token({"sub": "1"})
    assert decode_token(token) is None


def test_tampered_signature_rejected(rs256_keys):
    """Flip a byte in the signature segment → verify must fail."""
    from app.utils.security import create_access_token, decode_token

    token = create_access_token({"sub": "1"})
    head, payload, sig = token.split(".")
    # Twiddle one character in the signature; b64url alphabet so we
    # pick a safe substitution that stays in-charset.
    tampered_sig = ("A" if sig[0] != "A" else "B") + sig[1:]
    tampered = f"{head}.{payload}.{tampered_sig}"
    assert decode_token(tampered) is None


def test_payload_tampering_rejected(rs256_keys):
    """Editing the payload but keeping the original signature must
    fail — RSA signature covers head+payload as a unit."""
    from app.utils.security import create_access_token, decode_token

    token = create_access_token({"sub": "1"})
    head, _payload, sig = token.split(".")

    # Substitute a payload signed with the same private key but
    # different claims — naive substitution would break sig anyway,
    # but we exercise the more interesting case: keep the bytes from
    # a different token, glue it onto the original signature.
    other = create_access_token({"sub": "999", "username": "attacker"})
    _, other_payload, _ = other.split(".")
    forged = f"{head}.{other_payload}.{sig}"
    assert decode_token(forged) is None


def test_wrong_kid_rejected(rs256_keys):
    """A token signed by our private key but with a kid we don't
    publish (e.g. a stale rotation) must NOT verify."""
    from app.utils.security import decode_token, get_private_key

    payload = {
        "sub": "1",
        "type": "access",
        "exp": int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()),
    }
    token = jwt.encode(
        payload,
        get_private_key(),
        algorithm="RS256",
        headers={"kid": "rotated-out-key-v0", "typ": "JWT"},
    )
    assert decode_token(token) is None


def test_missing_kid_rejected(rs256_keys):
    """python-jose lets you skip ``kid`` — we don't. No kid means we
    can't reliably select a verification key, which is exactly the
    ambiguity algorithm-confusion attacks exploit. Always reject."""
    from app.utils.security import decode_token, get_private_key

    payload = {
        "sub": "1",
        "type": "access",
        "exp": int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()),
    }
    # No headers={} override → jose emits only alg/typ.
    token = jwt.encode(payload, get_private_key(), algorithm="RS256")
    header = jwt.get_unverified_header(token)
    assert "kid" not in header  # sanity-check the fixture
    assert decode_token(token) is None


def test_hs256_token_rejected(rs256_keys):
    """Algorithm-confusion classic: attacker ships a token with
    ``alg=HS256`` (any HMAC secret will do). Our verifier MUST reject
    the header itself via the ``algorithms=["RS256"]`` allowlist,
    before any key material is consulted.

    Note: python-jose now refuses to accept an asymmetric PEM as an
    HMAC secret at sign-time (it sanity-checks the key type), so the
    most direct shape of the attack — re-sign the public PEM with
    HS256 — is impossible to even craft. We exercise the next-most
    obvious variant: HS256 signed with an arbitrary attacker-chosen
    secret. With the allowlist in place our decode_token must still
    return None even though the signature is internally consistent.
    """
    from app.config import settings
    from app.utils.security import decode_token

    payload = {
        "sub": "1",
        "type": "access",
        "exp": int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()),
    }
    token = jwt.encode(
        payload,
        "attacker-chosen-hmac-secret",
        algorithm="HS256",
        headers={"kid": settings.JWT_KID, "typ": "JWT"},
    )
    assert decode_token(token) is None


def test_malformed_token_rejected(rs256_keys):
    """Random garbage must not raise — just return None."""
    from app.utils.security import decode_token

    assert decode_token("not.a.jwt") is None
    assert decode_token("") is None
    assert decode_token("aaaa.bbbb") is None


def test_secret_key_not_used_for_jwt_anymore(rs256_keys, monkeypatch):
    """Cutover contract: mutating SECRET_KEY must not affect verify.

    Belt-and-braces — if a future refactor accidentally re-introduces
    HS256 fallback, this test catches it.
    """
    from app.utils.security import create_access_token, decode_token
    from app.config import settings

    token = create_access_token({"sub": "1"})
    monkeypatch.setattr(settings, "SECRET_KEY", "rotated-to-something-else-entirely")
    payload = decode_token(token)
    assert payload is not None
    assert payload["sub"] == "1"
