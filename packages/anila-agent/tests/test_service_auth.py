"""service-wrapper 入向認證：派工 JWT fail-closed 契約（kill-proof）。

Production path is ``uvicorn anila_agent.serving.service_wrapper:app``,
which calls ``anila_agent.serving.auth`` — a thin facade over anila-core.
These tests pin the facade so removing signature / exp / iss / aud / alg /
unknown-kid-refetch in the shared verifier turns at least one test red.
"""

from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from anila_core.api.middleware.jwks_client import parse_jwks

from anila_agent.serving.auth import (
    DispatchAuthError,
    JwksClient,
    identity_from_claims,
    verify_dispatch_authorization,
    verify_dispatch_jwt,
    verify_service_token,
)

pytestmark = pytest.mark.unit


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _int_b64(value: int) -> str:
    length = (value.bit_length() + 7) // 8 or 1
    return _b64url(value.to_bytes(length, "big"))


def _sign(priv, kid: str, *, header_extra=None, **overrides):
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT", "kid": kid}
    if header_extra:
        header.update(header_extra)
    payload = {
        "iss": "anila-csp",
        "aud": "anila-agent",
        "sub": "7",
        "user_id": 7,
        "department": 3,
        "agent_id": 42,
        "iat": now,
        "exp": now + 300,
        "jti": "t",
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


def _client_with(doc: dict) -> JwksClient:
    async def fetch():
        return parse_jwks(doc)

    return JwksClient("https://csp.test/.well-known/jwks.json", fetch_fn=fetch)


def _jwks_doc(priv, kid: str) -> dict:
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


@pytest.mark.asyncio
async def test_valid_dispatch_token_accepted():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kid = "k1"
    client = _client_with(_jwks_doc(priv, kid))
    token = _sign(priv, kid)
    claims = await verify_dispatch_authorization(
        f"Bearer {token}", jwks_client=client
    )
    assert claims["user_id"] == 7
    ident = identity_from_claims(claims)
    assert ident["user_id"] == "7"
    assert ident["agent_id"] == 42


@pytest.mark.asyncio
async def test_missing_authorization_rejected():
    client = JwksClient("https://csp.test/.well-known/jwks.json")
    with pytest.raises(DispatchAuthError):
        await verify_dispatch_authorization(None, jwks_client=client)


@pytest.mark.asyncio
async def test_blank_jwks_url_fails_closed():
    client = JwksClient("")
    with pytest.raises(DispatchAuthError):
        await verify_dispatch_authorization(
            "Bearer x.y.z", jwks_client=client
        )


def test_legacy_static_token_helper_never_fail_open():
    assert verify_service_token("csk-abc", "csk-abc") is False
    assert verify_service_token(None, "", allow_unset=True) is False
    assert verify_service_token("x", "") is False


def test_signature_check_rejects_tampered_claim():
    """Kill: remove signature verify → this must go red."""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kid = "k1"
    token = _sign(priv, kid)
    h, p, s = token.split(".")
    pad = "=" * (-len(p) % 4)
    payload = json.loads(base64.urlsafe_b64decode(p + pad))
    payload["user_id"] = 999
    p2 = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    with pytest.raises(DispatchAuthError):
        verify_dispatch_jwt(f"{h}.{p2}.{s}", priv.public_key())


def test_expired_jwt_rejected():
    """Kill: remove exp check → this must go red."""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kid = "k1"
    past = int(time.time()) - 600
    token = _sign(priv, kid, iat=past, exp=past + 300)
    with pytest.raises(DispatchAuthError):
        verify_dispatch_jwt(token, priv.public_key())


def test_wrong_issuer_rejected():
    """Kill: remove iss check → this must go red."""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _sign(priv, "k1", iss="evil-issuer")
    with pytest.raises(DispatchAuthError):
        verify_dispatch_jwt(token, priv.public_key())


def test_wrong_audience_rejected():
    """Kill: remove aud check → this must go red."""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _sign(priv, "k1", aud="not-anila-agent")
    with pytest.raises(DispatchAuthError):
        verify_dispatch_jwt(token, priv.public_key())


def test_alg_pin_rejects_non_rs256():
    """Kill: remove RS256 alg pin → this must go red."""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    # Still RS256-signed body but claims alg=none / HS256 in header.
    for bad_alg in ("none", "None", "HS256", "PS256", "rs256"):
        token = _sign(priv, "k1", header_extra={"alg": bad_alg})
        with pytest.raises(DispatchAuthError):
            verify_dispatch_jwt(token, priv.public_key())


def test_unknown_signing_key_rejected():
    """Kill: remove signature verify → wrong-key token accepted."""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _sign(other, "k1")
    with pytest.raises(DispatchAuthError):
        verify_dispatch_jwt(token, priv.public_key())


def test_non_finite_exp_rejected():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    # 1e999 → inf via JSON number
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT", "kid": "k1"}
    payload = {
        "iss": "anila-csp",
        "aud": "anila-agent",
        "user_id": 7,
        "department": 3,
        "agent_id": 42,
        "iat": now,
        "exp": 1e999,
    }
    h = _b64url(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = priv.sign(
        f"{h}.{p}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    token = f"{h}.{p}.{_b64url(sig)}"
    with pytest.raises(DispatchAuthError):
        verify_dispatch_jwt(token, priv.public_key())


def test_malformed_exp_rejected():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _sign(priv, "k1", exp="not-a-number")
    with pytest.raises(DispatchAuthError):
        verify_dispatch_jwt(token, priv.public_key())


@pytest.mark.asyncio
async def test_unknown_kid_refetches_then_succeeds():
    """Kill: accept any kid without refetch → fetch_count stays flat / wrong key."""
    priv_old = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_new = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kid_old, kid_new = "kid-old", "kid-new"
    state = {"doc": _jwks_doc(priv_old, kid_old)}

    async def fetch():
        return parse_jwks(state["doc"])

    client = JwksClient("https://csp.test/.well-known/jwks.json", fetch_fn=fetch)
    old_token = _sign(priv_old, kid_old)
    new_token = _sign(priv_new, kid_new, user_id=11, department=None, agent_id=99)

    claims = await verify_dispatch_authorization(
        f"Bearer {old_token}", jwks_client=client
    )
    assert claims["user_id"] == 7
    assert client.fetch_count == 1

    state["doc"] = {
        "keys": _jwks_doc(priv_old, kid_old)["keys"]
        + _jwks_doc(priv_new, kid_new)["keys"]
    }
    before = client.fetch_count
    claims = await verify_dispatch_authorization(
        f"Bearer {new_token}", jwks_client=client
    )
    assert claims["agent_id"] == 99
    assert client.fetch_count == before + 1


@pytest.mark.asyncio
async def test_unknown_kid_absent_after_refetch_rejected():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    client = _client_with(_jwks_doc(priv, "known"))
    # Warm cache
    await verify_dispatch_authorization(
        f"Bearer {_sign(priv, 'known')}", jwks_client=client
    )
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(DispatchAuthError):
        await verify_dispatch_authorization(
            f"Bearer {_sign(other, 'missing-kid')}", jwks_client=client
        )
