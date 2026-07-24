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


def test_signature_failure_refreshes_jwks_after_key_rotation() -> None:
    """Same kid, new key material (CSP recreate) → forced refresh then accept."""
    old_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    new_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    new_private = new_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    old_public = jwk.construct(old_key.public_key(), algorithm="RS256").to_dict()
    new_public = jwk.construct(new_key.public_key(), algorithm="RS256").to_dict()
    token, body = _claims(private=new_private, kid="anila-v1")
    calls: list[int] = []
    published_old = {**old_public, "kid": "anila-v1", "alg": "RS256", "use": "sig"}
    published_new = {**new_public, "kid": "anila-v1", "alg": "RS256", "use": "sig"}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        published = published_old if len(calls) == 1 else published_new
        return httpx.Response(200, json={"keys": [published]}, request=request)

    verifier = RouterContextTokenVerifier(
        "https://csp.test/.well-known/jwks.json",
        issuer="https://csp.test/issuer",
        transport=httpx.MockTransport(handler),
    )
    # Prime cache with the retired public key under the same kid.
    asyncio.run(verifier._refresh())
    assert calls == [1]
    assert verifier.cached_kids == ("anila-v1",)

    result = asyncio.run(verifier.verify(token, body))
    assert result.jti == "jti-a"
    assert calls == [1, 1]


def test_forced_jwks_refresh_is_throttled(key_material) -> None:
    """Within the min interval, a second kid-miss must not re-hit JWKS."""
    private, public = key_material
    token, body = _claims(private=private, kid="kid-missing")
    clock = [1_000.0]
    calls: list[int] = []
    published = {**public, "kid": "kid-a", "alg": "RS256", "use": "sig"}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"keys": [published]}, request=request)

    verifier = RouterContextTokenVerifier(
        "https://csp.test/.well-known/jwks.json",
        issuer="https://csp.test/issuer",
        transport=httpx.MockTransport(handler),
        cache_ttl_seconds=300,
        force_refresh_min_interval_seconds=30.0,
        now=lambda: clock[0],
    )

    with pytest.raises(RouterContextVerificationError, match="kid 未發佈"):
        asyncio.run(verifier.verify(token, body))
    # Initial TTL miss + one forced refresh.
    assert calls == [1, 1]

    clock[0] += 10  # still inside the 30s throttle window
    with pytest.raises(RouterContextVerificationError, match="kid 未發佈"):
        asyncio.run(verifier.verify(token, body))
    assert calls == [1, 1]

    clock[0] += 25  # past throttle; allow another forced refresh
    with pytest.raises(RouterContextVerificationError, match="kid 未發佈"):
        asyncio.run(verifier.verify(token, body))
    assert calls == [1, 1, 1]


def test_forced_refresh_bypasses_throttle_when_cache_empty(key_material) -> None:
    """Empty JWKS cache exempts forced refresh from the global throttle."""
    private, public = key_material
    clock = [1_000.0]
    calls: list[int] = []
    published = {**public, "kid": "kid-a", "alg": "RS256", "use": "sig"}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"keys": [published]}, request=request)

    verifier = RouterContextTokenVerifier(
        "https://csp.test/.well-known/jwks.json",
        issuer="https://csp.test/issuer",
        transport=httpx.MockTransport(handler),
        cache_ttl_seconds=300,
        force_refresh_min_interval_seconds=30.0,
        now=lambda: clock[0],
    )

    # Populate cache and mark a recent forced refresh (throttle clock armed).
    assert asyncio.run(verifier._refresh()) is True
    verifier._last_forced_refresh_at = clock[0]
    assert calls == [1]

    clock[0] += 5  # still inside the 30s throttle window
    assert asyncio.run(verifier._refresh(force=True)) is False
    assert calls == [1]

    # Cleared cache → empty-cache exemption must re-fetch despite throttle.
    verifier._keys.clear()
    assert verifier.cached_kids == ()
    assert asyncio.run(verifier._refresh(force=True)) is True
    assert calls == [1, 1]
    assert verifier.cached_kids == ("kid-a",)
