"""P2.1 W1b: DispatchIdentityMiddleware acceptance locks.

(a) validly-signed token accepted; claims surface to the handler
(b) tampered claim → rejected
(c) expired token → rejected
(d) token signed by unknown key → rejected
(e) missing Authorization → rejected (fail-closed)
(f) unknown kid triggers exactly one JWKS refetch, then succeeds
(g) blank configuration still rejects (old fail-open hole gone)
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

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
from anila_core.api.middleware.jwks_client import JwksClient, parse_jwks


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _int_to_b64url(value: int) -> str:
    length = (value.bit_length() + 7) // 8 or 1
    return _b64url(value.to_bytes(length, "big"))


def _keypair() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks_doc(private_key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    nums = private_key.public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": kid,
                "n": _int_to_b64url(nums.n),
                "e": _int_to_b64url(nums.e),
            }
        ]
    }


def _sign(
    private_key: rsa.RSAPrivateKey,
    kid: str,
    *,
    user_id: int = 7,
    department: int | None = 3,
    agent_id: int = 42,
    iat: int | None = None,
    exp: int | None = None,
) -> str:
    now = int(time.time())
    iat_v = now if iat is None else iat
    exp_v = (now + 300) if exp is None else exp
    header = {"alg": "RS256", "typ": "JWT", "kid": kid}
    payload = {
        "iss": DISPATCH_TOKEN_ISSUER,
        "aud": DISPATCH_TOKEN_AUDIENCE,
        "sub": str(user_id),
        "user_id": user_id,
        "department": department,
        "agent_id": agent_id,
        "iat": iat_v,
        "exp": exp_v,
        "jti": "test-jti",
    }
    h = _b64url(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = private_key.sign(
        f"{h}.{p}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return f"{h}.{p}.{_b64url(sig)}"


def _build_app(jwks_client: JwksClient | None = None, **mw_kwargs: Any) -> FastAPI:
    app = FastAPI()
    if jwks_client is not None:
        mw_kwargs["jwks_client"] = jwks_client
    app.add_middleware(DispatchIdentityMiddleware, **mw_kwargs)

    @app.get("/foo")
    def foo(request: Request):
        return {"claims": getattr(request.state, "anila_dispatch", None)}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


@pytest.fixture
def rsa_kid():
    return _keypair(), "kid-test-1"


def test_a_valid_token_accepted_and_claims_surface(rsa_kid):
    priv, kid = rsa_kid
    doc = _jwks_doc(priv, kid)

    async def fetch():
        return parse_jwks(doc)

    client_jwks = JwksClient("https://csp.test/.well-known/jwks.json", fetch_fn=fetch)
    app = _build_app(client_jwks)
    token = _sign(priv, kid, user_id=7, department=3, agent_id=42)
    with TestClient(app) as c:
        r = c.get("/foo", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    claims = r.json()["claims"]
    assert claims["user_id"] == 7
    assert claims["department"] == 3
    assert claims["agent_id"] == 42


def test_b_tampered_claim_rejected(rsa_kid):
    priv, kid = rsa_kid
    doc = _jwks_doc(priv, kid)

    async def fetch():
        return parse_jwks(doc)

    client_jwks = JwksClient("https://csp.test/.well-known/jwks.json", fetch_fn=fetch)
    app = _build_app(client_jwks)
    token = _sign(priv, kid)
    header_b64, payload_b64, sig_b64 = token.split(".")
    pad = "=" * (-len(payload_b64) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload_b64 + pad))
    claims["user_id"] = 999
    new_payload = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    tampered = f"{header_b64}.{new_payload}.{sig_b64}"
    with TestClient(app) as c:
        r = c.get("/foo", headers={"Authorization": f"Bearer {tampered}"})
    assert r.status_code == 401


def test_c_expired_token_rejected(rsa_kid):
    priv, kid = rsa_kid
    doc = _jwks_doc(priv, kid)

    async def fetch():
        return parse_jwks(doc)

    client_jwks = JwksClient("https://csp.test/.well-known/jwks.json", fetch_fn=fetch)
    app = _build_app(client_jwks)
    past = int(time.time()) - 600
    token = _sign(priv, kid, iat=past, exp=past + 300)
    with TestClient(app) as c:
        r = c.get("/foo", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_d_unknown_signing_key_rejected(rsa_kid):
    priv, kid = rsa_kid
    other = _keypair()
    doc = _jwks_doc(priv, kid)  # published key ≠ signer

    async def fetch():
        return parse_jwks(doc)

    client_jwks = JwksClient("https://csp.test/.well-known/jwks.json", fetch_fn=fetch)
    app = _build_app(client_jwks)
    token = _sign(other, kid)  # same kid claim, wrong key
    with TestClient(app) as c:
        r = c.get("/foo", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_e_missing_authorization_rejected(rsa_kid):
    priv, kid = rsa_kid
    doc = _jwks_doc(priv, kid)

    async def fetch():
        return parse_jwks(doc)

    client_jwks = JwksClient("https://csp.test/.well-known/jwks.json", fetch_fn=fetch)
    app = _build_app(client_jwks)
    with TestClient(app) as c:
        r = c.get("/foo")
    assert r.status_code == 401


def test_f_unknown_kid_refetches_once_then_succeeds():
    priv_old, kid_old = _keypair(), "kid-old"
    priv_new, kid_new = _keypair(), "kid-new"
    state = {"doc": _jwks_doc(priv_old, kid_old)}

    async def fetch():
        return parse_jwks(state["doc"])

    client_jwks = JwksClient("https://csp.test/.well-known/jwks.json", fetch_fn=fetch)
    app = _build_app(client_jwks)
    old_token = _sign(priv_old, kid_old)
    new_token = _sign(priv_new, kid_new, user_id=11, department=None, agent_id=99)
    with TestClient(app) as c:
        r = c.get("/foo", headers={"Authorization": f"Bearer {old_token}"})
        assert r.status_code == 200
        assert client_jwks.fetch_count == 1
        # Publish the new key; next unknown-kid lookup must refetch once.
        state["doc"] = {
            "keys": _jwks_doc(priv_old, kid_old)["keys"]
            + _jwks_doc(priv_new, kid_new)["keys"]
        }
        before = client_jwks.fetch_count
        r = c.get("/foo", headers={"Authorization": f"Bearer {new_token}"})
        assert r.status_code == 200
        assert r.json()["claims"]["agent_id"] == 99
        assert client_jwks.fetch_count == before + 1


def test_g_blank_configuration_rejects_not_fail_open():
    """Old hole: empty service_token ⇒ pass-through. Must not survive."""
    app = _build_app(csp_base_url="", jwks_url="")
    with TestClient(app) as c:
        r = c.get("/foo")
    assert r.status_code == 401
    # Even with a junk Bearer, blank JWKS config must not pass.
    with TestClient(app) as c:
        r = c.get("/foo", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401


def test_health_bypasses_auth():
    app = _build_app(csp_base_url="")
    with TestClient(app) as c:
        r = c.get("/health")
    assert r.status_code == 200


def test_no_ssl_cert_file_in_jwks_client_source():
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src/anila_core/api/middleware"
    for path in src.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        # Mentions in comments/docs warning about the footgun are OK;
        # executable use of os.environ["SSL_CERT_FILE"] / getenv is not.
        assert 'getenv("SSL_CERT_FILE"' not in text
        assert "environ[\"SSL_CERT_FILE\"]" not in text
        assert "environ['SSL_CERT_FILE']" not in text
