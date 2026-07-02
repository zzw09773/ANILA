"""Tests for anila-studio/app/auth.py — local JWT verify + revocation check.

These run as unit tests against the FastAPI dependency directly so they
do not need lifespan setup. The jose / cryptography / RSA round-trip is
real (small RSA key generated per test session); jwks_client and
revocation_cache are stubbed at the module boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt

from app import auth as auth_mod


# ── RSA key pair for round-trip signing ──────────────────────────────────


@pytest.fixture(scope="module")
def rsa_keypair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key()
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return {
        "private_pem": private_pem,
        "public_key_obj": public,
    }


def _sign_jwt(
    private_pem: bytes,
    *,
    sub: str = "42",
    username: str = "alice",
    role: str = "user",
    token_version: int = 0,
    type_: str = "access",
    kid: str = "anila-v1",
    exp: int | None = None,
) -> str:
    import time

    claims = {
        "sub": sub,
        "username": username,
        "role": role,
        "tv": token_version,
        "type": type_,
        "iat": int(time.time()),
        "exp": exp if exp is not None else int(time.time()) + 3600,
    }
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": kid})


# ── Fixture: patch jwks_client + revocation_cache module ─────────────────


@pytest.fixture
def patched_deps(monkeypatch, rsa_keypair):
    """Patch jwks_client.get_public_key to return the test RSA pub key and
    revocation_cache.get_revocation_cache to return a controllable fake.
    """
    from app.services import jwks_client as jwks_mod
    from app.services import revocation_cache as rev_mod

    async def fake_get_public_key(kid: str):
        if kid != "anila-v1":
            raise jwks_mod.JwksKeyNotFoundError(kid)
        return rsa_keypair["public_key_obj"]

    monkeypatch.setattr(jwks_mod, "get_public_key", fake_get_public_key)

    @dataclass
    class _FakeCache:
        revoked_set: set
        ready_flag: bool = True

        @property
        def ready(self) -> bool:
            return self.ready_flag

        async def is_revoked(self, user_id: int, token_version: int) -> bool:
            return (user_id, token_version) in self.revoked_set

    fake_cache = _FakeCache(revoked_set=set())
    monkeypatch.setattr(rev_mod, "get_revocation_cache", lambda: fake_cache)
    return fake_cache


# ── App harness ──────────────────────────────────────────────────────────


@pytest.fixture
def app():
    from app.auth import get_current_user_identity

    test_app = FastAPI()

    @test_app.get("/whoami")
    async def whoami(identity=pytest.importorskip("fastapi").Depends(
        get_current_user_identity,
    )):
        return {
            "id": identity.id,
            "username": identity.username,
            "role": identity.role,
            "token_version": identity.token_version,
        }

    return test_app


@pytest.fixture
def client(app):
    return TestClient(app)


# ── Tests ────────────────────────────────────────────────────────────────


def test_valid_token_passes(client, patched_deps, rsa_keypair):
    token = _sign_jwt(rsa_keypair["private_pem"], sub="42", role="user")
    resp = client.get(
        "/whoami", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == 42
    assert body["username"] == "alice"
    assert body["role"] == "user"
    assert body["token_version"] == 0


def test_cookie_token_also_works(client, patched_deps, rsa_keypair):
    token = _sign_jwt(rsa_keypair["private_pem"], sub="42")
    resp = client.get(
        "/whoami",
        cookies={auth_mod.ACCESS_COOKIE_NAME: token},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == 42


def test_bearer_wins_when_both_present(client, patched_deps, rsa_keypair):
    bearer_token = _sign_jwt(rsa_keypair["private_pem"], sub="99", username="bob")
    cookie_token = _sign_jwt(rsa_keypair["private_pem"], sub="42")
    resp = client.get(
        "/whoami",
        headers={"Authorization": f"Bearer {bearer_token}"},
        cookies={auth_mod.ACCESS_COOKIE_NAME: cookie_token},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == 99
    assert resp.json()["username"] == "bob"


def test_missing_token_returns_401(client, patched_deps):
    resp = client.get("/whoami")
    assert resp.status_code == 401


def test_expired_token_returns_401(client, patched_deps, rsa_keypair):
    import time

    token = _sign_jwt(
        rsa_keypair["private_pem"], exp=int(time.time()) - 60
    )
    resp = client.get(
        "/whoami", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401


def test_token_signed_with_wrong_key_returns_401(client, patched_deps, rsa_keypair):
    # Sign with a *different* private key but the same kid — JWKS public key
    # should reject the signature.
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_pem = other.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    token = _sign_jwt(other_pem)
    resp = client.get(
        "/whoami", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401


def test_token_without_kid_header_rejected(client, patched_deps, rsa_keypair):
    """Algorithm-confusion defence: refuse tokens missing the kid header."""
    import time

    claims = {
        "sub": "42", "username": "alice", "role": "user",
        "tv": 0, "type": "access",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    token = jwt.encode(claims, rsa_keypair["private_pem"], algorithm="RS256")
    # No kid in header
    resp = client.get(
        "/whoami", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401


def test_refresh_token_rejected_on_studio_surface(client, patched_deps, rsa_keypair):
    """A type=refresh token must NOT authenticate studio API calls."""
    token = _sign_jwt(rsa_keypair["private_pem"], type_="refresh")
    resp = client.get(
        "/whoami", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401


def test_revoked_token_returns_401(client, patched_deps, rsa_keypair):
    """Token version matches a revocation entry → 401."""
    patched_deps.revoked_set.add((42, 0))
    token = _sign_jwt(rsa_keypair["private_pem"], sub="42", token_version=0)
    resp = client.get(
        "/whoami", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401


def test_revocation_cache_not_ready_returns_503(client, patched_deps, rsa_keypair):
    """fail-closed: when Redis-backed deny-list is unhealthy, refuse auth."""
    patched_deps.ready_flag = False
    token = _sign_jwt(rsa_keypair["private_pem"])
    resp = client.get(
        "/whoami", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 503


def test_unknown_kid_returns_401(client, patched_deps, rsa_keypair):
    token = _sign_jwt(rsa_keypair["private_pem"], kid="anila-v2")
    resp = client.get(
        "/whoami", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401


def test_jwks_fetch_error_returns_503(
    client, patched_deps, rsa_keypair, monkeypatch,
):
    """If JWKS itself is unreachable, treat as service degraded (503)."""
    from app.services import jwks_client as jwks_mod

    async def boom_get(kid):
        raise jwks_mod.JwksFetchError("csp unreachable")

    monkeypatch.setattr(jwks_mod, "get_public_key", boom_get)
    token = _sign_jwt(rsa_keypair["private_pem"])
    resp = client.get(
        "/whoami", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 503


def test_invalid_sub_returns_401(client, patched_deps, rsa_keypair):
    token = _sign_jwt(rsa_keypair["private_pem"], sub="not-a-number")
    resp = client.get(
        "/whoami", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401


def test_get_bearer_token_dependency_returns_raw_token(patched_deps, rsa_keypair):
    """Bearer dependency returns the bearer string for csp_client passthrough."""
    from app.auth import get_bearer_token

    test_app = FastAPI()

    @test_app.get("/token-echo")
    async def echo(token: str = pytest.importorskip("fastapi").Depends(
        get_bearer_token,
    )):
        return {"token_prefix": token[:8]}

    c = TestClient(test_app)
    token = _sign_jwt(rsa_keypair["private_pem"])
    resp = c.get("/token-echo", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["token_prefix"] == token[:8]
