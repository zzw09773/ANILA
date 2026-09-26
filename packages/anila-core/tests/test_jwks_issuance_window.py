"""驗證端要看每把鑰匙的簽發期間，不能只因為 kid 在 JWKS 裡就放行。"""
from __future__ import annotations

import base64
import importlib.util
import json
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from anila_core.api.middleware.dispatch_auth import DispatchIdentityMiddleware
from anila_core.api.middleware.dispatch_jwt import (
    DISPATCH_TOKEN_AUDIENCE,
    DISPATCH_TOKEN_ISSUER,
)
from anila_core.api.middleware.jwks_client import JwksClient


VERIFY_PATH = (
    Path(__file__).resolve().parents[1] / "src/anila_core/contrib/anila_verify.py"
)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _int_b64(value: int) -> str:
    length = (value.bit_length() + 7) // 8 or 1
    return _b64url(value.to_bytes(length, "big"))


def _load_standalone():
    spec = importlib.util.spec_from_file_location("anila_verify_issuance", VERIFY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sign(private_key, kid: str, *, iat: int | None, exp: int) -> str:
    header = {"alg": "RS256", "typ": "JWT", "kid": kid}
    payload = {
        "iss": DISPATCH_TOKEN_ISSUER,
        "aud": DISPATCH_TOKEN_AUDIENCE,
        "sub": "7",
        "user_id": 7,
        "department": 3,
        "agent_id": 42,
        "exp": exp,
        "jti": "window",
    }
    if iat is not None:
        payload["iat"] = iat
    header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signature = private_key.sign(
        f"{header_b64}.{payload_b64}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return f"{header_b64}.{payload_b64}.{_b64url(signature)}"


def _document(private_key, kid: str, **extra) -> dict:
    numbers = private_key.public_key().public_numbers()
    entry = {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _int_b64(numbers.n),
        "e": _int_b64(numbers.e),
    }
    entry.update(extra)
    return {"keys": [entry]}


def test_standalone_verifier_rejects_next_key_and_late_retiring_iat():
    module = _load_standalone()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    next_keys = module.parse_jwks(
        _document(
            private_key,
            "next-kid",
            anila_key_state="next",
            anila_accept_missing_iat=False,
        )
    )
    next_token = _sign(private_key, "next-kid", iat=now, exp=now + 120)
    with pytest.raises(module.AnilaVerifyError):
        module.verify_authorization(f"Bearer {next_token}", jwks=next_keys)

    cutoff = now - 30
    retiring_keys = module.parse_jwks(
        _document(
            private_key,
            "old-kid",
            anila_key_state="retiring",
            anila_iat_not_after=cutoff,
            anila_accept_missing_iat=True,
        )
    )
    late = _sign(private_key, "old-kid", iat=cutoff, exp=now + 120)
    with pytest.raises(module.AnilaVerifyError):
        module.verify_authorization(f"Bearer {late}", jwks=retiring_keys)
    early = _sign(private_key, "old-kid", iat=cutoff - 5, exp=now + 120)
    assert module.verify_authorization(f"Bearer {early}", jwks=retiring_keys)["user_id"] == 7
    legacy = _sign(private_key, "old-kid", iat=None, exp=now + 120)
    assert module.verify_authorization(f"Bearer {legacy}", jwks=retiring_keys)["agent_id"] == 42


def test_dispatch_middleware_rejects_next_key():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    document = _document(
        private_key,
        "next-kid",
        anila_key_state="next",
        anila_accept_missing_iat=False,
    )

    async def fetch():
        from anila_core.api.middleware.jwks_client import parse_jwks

        return parse_jwks(document)

    app = FastAPI()
    app.add_middleware(
        DispatchIdentityMiddleware,
        jwks_client=JwksClient(
            "https://csp.test/.well-known/jwks.json",
            fetch_fn=fetch,
        ),
    )

    @app.get("/foo")
    def foo(request: Request):
        return {"claims": getattr(request.state, "anila_dispatch", None)}

    token = _sign(private_key, "next-kid", iat=now, exp=now + 120)
    with TestClient(app) as client:
        response = client.get("/foo", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
