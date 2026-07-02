"""Sprint 9 / anila-studio extraction: GET /.well-known/jwks.json.

Contract pinned here:

* 200 OK, JSON body.
* Body shape matches RFC 7517 — single ``keys`` array, each entry has
  ``kty`` / ``use`` / ``alg`` / ``kid`` / ``n`` / ``e``.
* ``Cache-Control`` honoured so downstream verifiers cache locally.
* The published modulus ``n`` actually validates a token signed by
  CSP — i.e. the JWKS is functionally end-to-end correct, not just
  shape-correct.

Tests bypass the main FastAPI app so we don't depend on whether the
main thread has wired the jwks router in yet — the router is a
stand-alone APIRouter and can be mounted on a throwaway app.
"""
from __future__ import annotations

import base64
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def rs256_keys(tmp_path: Path, monkeypatch):
    """Same provisioning logic as test_rs256_jwt — kept independent so
    each suite can be invoked in isolation."""
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
    yield private_key
    sec_module._load_keys.cache_clear()


@pytest.fixture
def jwks_client(rs256_keys) -> TestClient:
    """Stand up a minimal FastAPI app with ONLY the jwks router so
    middleware / lifespan from the main app don't intrude."""
    from app.api.jwks import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _b64url_decode_int(value: str) -> int:
    """Inverse of ``_int_to_base64url`` — pads, decodes, reads big-endian."""
    pad = "=" * (-len(value) % 4)
    raw = base64.urlsafe_b64decode(value + pad)
    return int.from_bytes(raw, "big")


def test_jwks_endpoint_returns_200(jwks_client):
    response = jwks_client.get("/.well-known/jwks.json")
    assert response.status_code == 200


def test_jwks_endpoint_returns_json_content_type(jwks_client):
    response = jwks_client.get("/.well-known/jwks.json")
    assert response.headers["content-type"].startswith("application/json")


def test_jwks_endpoint_cache_control_header(jwks_client):
    response = jwks_client.get("/.well-known/jwks.json")
    cache_control = response.headers.get("cache-control", "")
    assert "max-age=3600" in cache_control


def test_jwks_response_shape_matches_rfc7517(jwks_client):
    """Single key today — array shape must be future-proof for rotation."""
    response = jwks_client.get("/.well-known/jwks.json")
    body = response.json()

    assert isinstance(body, dict)
    assert list(body.keys()) == ["keys"]
    assert isinstance(body["keys"], list)
    assert len(body["keys"]) == 1


def test_jwks_key_fields_complete(jwks_client):
    response = jwks_client.get("/.well-known/jwks.json")
    key = response.json()["keys"][0]

    assert key["kty"] == "RSA"
    assert key["use"] == "sig"
    assert key["alg"] == "RS256"
    assert "kid" in key
    assert "n" in key
    assert "e" in key


def test_jwks_kid_matches_configured(jwks_client):
    from app.config import settings

    response = jwks_client.get("/.well-known/jwks.json")
    assert response.json()["keys"][0]["kid"] == settings.JWT_KID


def test_jwks_modulus_is_base64url_without_padding(jwks_client):
    """RFC 7518 §6.3.1 — ``n`` / ``e`` use base64url with NO padding."""
    response = jwks_client.get("/.well-known/jwks.json")
    key = response.json()["keys"][0]
    assert "=" not in key["n"], "modulus must be unpadded base64url"
    assert "=" not in key["e"], "exponent must be unpadded base64url"


def test_jwks_public_exponent_is_65537(jwks_client):
    """We always generate with e=65537; pin so a regression in keygen
    is obvious."""
    response = jwks_client.get("/.well-known/jwks.json")
    e = _b64url_decode_int(response.json()["keys"][0]["e"])
    assert e == 65537


def test_jwks_modulus_matches_actual_public_key(jwks_client, rs256_keys):
    """The ``n`` field must equal the modulus of the configured
    private key's public half — otherwise downstream verifiers will
    reject CSP-signed tokens despite the endpoint returning 200."""
    response = jwks_client.get("/.well-known/jwks.json")
    n_from_jwks = _b64url_decode_int(response.json()["keys"][0]["n"])
    actual_modulus = rs256_keys.public_key().public_numbers().n
    assert n_from_jwks == actual_modulus


def test_jwks_published_key_validates_csp_signed_token(jwks_client):
    """End-to-end:

    1. Fetch JWKS over HTTP.
    2. Reconstruct the public key from ``n`` / ``e``.
    3. Use that public key to verify a token CSP just signed.

    If this passes, anila-studio (which does exactly this) will too.
    """
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
    from jose import jwt
    from app.config import settings
    from app.utils.security import create_access_token

    response = jwks_client.get("/.well-known/jwks.json")
    key = response.json()["keys"][0]
    n = _b64url_decode_int(key["n"])
    e = _b64url_decode_int(key["e"])
    reconstructed_pub = RSAPublicNumbers(e=e, n=n).public_key()
    reconstructed_pem = reconstructed_pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    token = create_access_token({"sub": "42"})
    payload = jwt.decode(token, reconstructed_pem, algorithms=["RS256"])
    assert payload["sub"] == "42"


def test_jwks_endpoint_does_not_require_auth(jwks_client):
    """Public keys are public — no Authorization / cookie should be needed."""
    response = jwks_client.get("/.well-known/jwks.json")
    assert response.status_code == 200


def test_jwks_endpoint_idempotent(jwks_client):
    """Two GETs must return byte-identical keys array (the modulus
    can't drift between calls in the same process)."""
    r1 = jwks_client.get("/.well-known/jwks.json").json()
    r2 = jwks_client.get("/.well-known/jwks.json").json()
    assert r1 == r2
