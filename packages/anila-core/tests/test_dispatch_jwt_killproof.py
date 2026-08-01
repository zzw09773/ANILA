"""Kill-proof unit tests for anila_core dispatch JWT + JWKS.

INVARIANT: removing signature / exp / iss / aud / RS256 pin / unknown-kid
refetch / non-finite-exp rejection must turn at least one test red.
"""

from __future__ import annotations

import base64
import json
import math
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from anila_core.api.middleware.dispatch_jwt import (
    DISPATCH_TOKEN_AUDIENCE,
    DISPATCH_TOKEN_ISSUER,
    DispatchTokenError,
    verify_dispatch_jwt,
)
from anila_core.api.middleware.jwks_client import JwksClient, JwksFetchError, parse_jwks


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _int_b64(value: int) -> str:
    length = (value.bit_length() + 7) // 8 or 1
    return _b64url(value.to_bytes(length, "big"))


def _sign(priv, kid: str = "k1", *, header_extra=None, **overrides):
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT", "kid": kid}
    if header_extra:
        header.update(header_extra)
    payload = {
        "iss": DISPATCH_TOKEN_ISSUER,
        "aud": DISPATCH_TOKEN_AUDIENCE,
        "sub": "7",
        "user_id": 7,
        "department": 3,
        "agent_id": 42,
        "iat": now,
        "exp": now + 300,
        "jti": "kill",
    }
    payload.update(overrides)
    h = _b64url(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = priv.sign(
        f"{h}.{p}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return f"{h}.{p}.{_b64url(sig)}"


def test_valid_token_accepted():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = verify_dispatch_jwt(_sign(priv), priv.public_key())
    assert claims["user_id"] == 7


def test_kill_signature_tampered_claim():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _sign(priv)
    h, p, s = token.split(".")
    pad = "=" * (-len(p) % 4)
    payload = json.loads(base64.urlsafe_b64decode(p + pad))
    payload["user_id"] = 999
    p2 = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    with pytest.raises(DispatchTokenError):
        verify_dispatch_jwt(f"{h}.{p2}.{s}", priv.public_key())


def test_kill_signature_wrong_key():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(DispatchTokenError):
        verify_dispatch_jwt(_sign(other), priv.public_key())


def test_kill_exp_check():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    past = int(time.time()) - 600
    with pytest.raises(DispatchTokenError):
        verify_dispatch_jwt(_sign(priv, iat=past, exp=past + 300), priv.public_key())


def test_kill_iss_check():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(DispatchTokenError):
        verify_dispatch_jwt(_sign(priv, iss="evil"), priv.public_key())


def test_kill_aud_check():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(DispatchTokenError):
        verify_dispatch_jwt(_sign(priv, aud="other"), priv.public_key())


def test_kill_rs256_alg_pin():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for bad in ("none", "HS256", "PS256", "rs256"):
        with pytest.raises(DispatchTokenError):
            verify_dispatch_jwt(
                _sign(priv, header_extra={"alg": bad}), priv.public_key()
            )


def test_kill_non_finite_exp():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT", "kid": "k1"}
    payload = {
        "iss": DISPATCH_TOKEN_ISSUER,
        "aud": DISPATCH_TOKEN_AUDIENCE,
        "user_id": 7,
        "department": 3,
        "agent_id": 42,
        "iat": now,
        "exp": 1e999,
    }
    assert math.isinf(float(payload["exp"]))
    h = _b64url(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = priv.sign(
        f"{h}.{p}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    with pytest.raises(DispatchTokenError):
        verify_dispatch_jwt(f"{h}.{p}.{_b64url(sig)}", priv.public_key())


def test_malformed_exp_rejected():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(DispatchTokenError):
        verify_dispatch_jwt(_sign(priv, exp="nope"), priv.public_key())


@pytest.mark.asyncio
async def test_kill_unknown_kid_refetch():
    priv_old = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_new = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kid_old, kid_new = "old", "new"

    def _doc(priv, kid):
        nums = priv.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": kid,
                    "n": _int_b64(nums.n),
                    "e": _int_b64(nums.e),
                }
            ]
        }

    state = {"doc": _doc(priv_old, kid_old)}

    async def fetch():
        return parse_jwks(state["doc"])

    client = JwksClient("https://csp.test/.well-known/jwks.json", fetch_fn=fetch)
    await client.get_public_key(kid_old)
    assert client.fetch_count == 1

    state["doc"] = {
        "keys": _doc(priv_old, kid_old)["keys"] + _doc(priv_new, kid_new)["keys"]
    }
    before = client.fetch_count
    key = await client.get_public_key(kid_new)
    assert key is not None
    assert client.fetch_count == before + 1


@pytest.mark.asyncio
async def test_ca_file_oserror_is_jwks_fetch_error(tmp_path):
    missing = tmp_path / "no-such-ca.pem"
    client = JwksClient(
        "https://example.test/.well-known/jwks.json",
        ca_file=str(missing),
    )
    with pytest.raises(JwksFetchError):
        await client.get_public_key("any")
