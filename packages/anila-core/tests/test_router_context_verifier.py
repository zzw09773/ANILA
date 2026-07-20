"""Focused Router JWKS/token provenance verification tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt

from anila_core.security.router_context import (
    RouterContextTokenVerifier,
    RouterContextVerificationError,
)


@pytest.fixture
def key_material():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public = jwk.construct(key.public_key(), algorithm="RS256").to_dict()
    return private, public


def _claims(*, private: bytes, kid: str = "kid-a", **overrides):
    body = {
        "model": "anila-router",
        "messages": [{"role": "user", "content": "hello"}],
        "anila_session_id": "session-a",
    }
    now = int(datetime.now(timezone.utc).timestamp())
    payload = {
        "iss": "https://csp.test/issuer",
        "aud": "anila-router",
        "iat": now,
        "exp": now + 60,
        "jti": "jti-a",
        "type": "router-context/v1",
        "sub": "42",
        "caller_user_id": "42",
        "owner_id": "42",
        "task_id": "11",
        "run_id": "12",
        "source_snapshot_id": "13",
        "trace_id": "trace-a",
        "invocation_id": "invocation-a",
        "session_id": "session-a",
        "task_type": "query",
        "classification": "無機密",
        "scopes": ["agent:invoke"],
        "required_capabilities": ["retrieval"],
        "auth_assurance": {
            "sid": "auth-session",
            "amr": ["pwd"],
            "acr": "aal2",
            "auth_time": "2026-07-15T00:00:00+00:00",
            "break_glass": False,
        },
        "body_sha256": "",
    }
    from anila_core.security.router_context import canonical_router_body_sha256

    payload["body_sha256"] = canonical_router_body_sha256(body)
    payload.update(overrides)
    return jwt.encode(
        payload,
        private,
        algorithm="RS256",
        headers={"kid": kid, "typ": "anila-router-context"},
    ), body


def _transport(public: dict, *, kid: str = "kid-a", calls: list[int] | None = None):
    published = {**public, "kid": kid, "alg": "RS256", "use": "sig"}

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(1)
        return httpx.Response(200, json={"keys": [published]}, request=request)

    return httpx.MockTransport(handler)


def test_verifier_accepts_valid_token_and_caches_jwks(key_material) -> None:
    private, public = key_material
    token, body = _claims(private=private)
    calls: list[int] = []
    verifier = RouterContextTokenVerifier(
        "https://csp.test/.well-known/jwks.json",
        issuer="https://csp.test/issuer",
        transport=_transport(public, calls=calls),
    )

    first = asyncio.run(verifier.verify(token, body))
    second = asyncio.run(verifier.verify(token, body))
    assert first.caller_user_id == second.caller_user_id == "42"
    assert calls == [1]
    assert verifier.cached_kids == ("kid-a",)


@pytest.mark.parametrize(
    "overrides",
    [
        {"sub": "99"},
        {"type": "JWT"},
        {"aud": "wrong-audience"},
        {"session_id": "other-session"},
        {"body_sha256": "0" * 64},
    ],
)
def test_verifier_rejects_signed_claim_or_body_tamper(key_material, overrides) -> None:
    private, public = key_material
    token, body = _claims(private=private, **overrides)
    verifier = RouterContextTokenVerifier(
        "https://csp.test/.well-known/jwks.json",
        issuer="https://csp.test/issuer",
        transport=_transport(public),
    )
    with pytest.raises(RouterContextVerificationError):
        asyncio.run(verifier.verify(token, body))


def test_unknown_kid_forces_one_refresh_and_accepts_overlap(key_material) -> None:
    private, public = key_material
    token, body = _claims(private=private, kid="kid-b")
    calls: list[int] = []
    published_a = {**public, "kid": "kid-a", "alg": "RS256", "use": "sig"}
    published_b = {**public, "kid": "kid-b", "alg": "RS256", "use": "sig"}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        # First publication has the old key; forced refresh publishes both.
        keys = [published_a] if len(calls) == 1 else [published_a, published_b]
        return httpx.Response(200, json={"keys": keys}, request=request)

    verifier = RouterContextTokenVerifier(
        "https://csp.test/.well-known/jwks.json",
        issuer="https://csp.test/issuer",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(verifier.verify(token, body))
    assert result.jti == "jti-a"
    assert calls == [1, 1]
    assert verifier.cached_kids == ("kid-a", "kid-b")


def test_removed_kid_is_rejected_after_cache_refresh(key_material) -> None:
    private, public = key_material
    token, body = _claims(private=private, kid="kid-a")
    clock = [datetime.now(timezone.utc).timestamp()]
    calls: list[int] = []
    published_a = {**public, "kid": "kid-a", "alg": "RS256", "use": "sig"}
    published_b = {**public, "kid": "kid-b", "alg": "RS256", "use": "sig"}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        published = published_a if len(calls) == 1 else published_b
        return httpx.Response(200, json={"keys": [published]}, request=request)

    verifier = RouterContextTokenVerifier(
        "https://csp.test/.well-known/jwks.json",
        issuer="https://csp.test/issuer",
        transport=httpx.MockTransport(handler),
        cache_ttl_seconds=5,
        now=lambda: clock[0],
    )

    assert asyncio.run(verifier.verify(token, body)).jti == "jti-a"
    assert verifier.cached_kids == ("kid-a",)

    # The old key is removed from the published set.  The normal TTL refresh
    # and the one allowed unknown-kid refresh must both observe that removal.
    clock[0] += 10
    with pytest.raises(RouterContextVerificationError):
        asyncio.run(verifier.verify(token, body))
    assert calls == [1, 1, 1]
    assert verifier.cached_kids == ("kid-b",)
