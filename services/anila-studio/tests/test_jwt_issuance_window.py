"""JWKS 公布 next，但升成 active 之前不能拿來驗權杖。"""
from __future__ import annotations

import time

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jose import jwt

from app.config import settings


def _b64url_int(value: int) -> str:
    length = (value.bit_length() + 7) // 8 or 1
    raw = value.to_bytes(length, "big")
    import base64

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _document(private_key, kid: str, **extra) -> dict:
    numbers = private_key.public_key().public_numbers()
    entry = {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _b64url_int(numbers.n),
        "e": _b64url_int(numbers.e),
    }
    entry.update(extra)
    return {"keys": [entry]}


def _token(private_key, kid: str, *, iat: int | None) -> str:
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    now = int(time.time())
    claims = {
        "sub": "7",
        "username": "alice",
        "role": "user",
        "tv": 0,
        "type": "access",
        "exp": now + 300,
    }
    if iat is not None:
        claims["iat"] = iat
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": kid})


@pytest.mark.asyncio
async def test_next_key_document_does_not_verify(monkeypatch):
    from app.auth import _verify_jwt
    from app.services import jwks_client as jwks_mod

    monkeypatch.setattr(settings, "CSP_BASE_URL", "http://csp-test:8000")
    monkeypatch.setattr(settings, "JWKS_REFRESH_SECONDS", 3600)
    jwks_mod._reset_for_tests()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    document = _document(
        private_key,
        "anila-next",
        anila_key_state="next",
        anila_accept_missing_iat=False,
    )
    token = _token(private_key, "anila-next", iat=int(time.time()))
    with respx.mock(base_url="http://csp-test:8000") as router:
        router.get("/.well-known/jwks.json").mock(
            return_value=httpx.Response(200, json=document)
        )
        with pytest.raises(HTTPException) as raised:
            await _verify_jwt(token)
    assert raised.value.status_code == 401


@pytest.mark.asyncio
async def test_retiring_key_rejects_iat_on_or_after_cutoff(monkeypatch):
    from app.auth import _verify_jwt
    from app.services import jwks_client as jwks_mod

    monkeypatch.setattr(settings, "CSP_BASE_URL", "http://csp-test:8000")
    monkeypatch.setattr(settings, "JWKS_REFRESH_SECONDS", 3600)
    jwks_mod._reset_for_tests()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cutoff = int(time.time()) - 30
    document = _document(
        private_key,
        "anila-old",
        anila_key_state="retiring",
        anila_iat_not_after=cutoff,
        anila_accept_missing_iat=False,
    )
    token = _token(private_key, "anila-old", iat=cutoff)
    with respx.mock(base_url="http://csp-test:8000") as router:
        router.get("/.well-known/jwks.json").mock(
            return_value=httpx.Response(200, json=document)
        )
        with pytest.raises(HTTPException) as raised:
            await _verify_jwt(token)
    assert raised.value.status_code == 401
