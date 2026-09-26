"""Tests for app.services.jwks_client.

Contract pinned by these tests:

* Cold-start lazily fetches ``{CSP_BASE_URL}/.well-known/jwks.json`` only on
  first lookup (no network at import).
* Result is cached in-memory; subsequent lookups for the same ``kid`` do not
  re-issue HTTP.
* A miss (kid not in cache) forces exactly one refetch; if still missing,
  raises :class:`JwksKeyNotFoundError` instead of returning ``None``.
* HTTP failures map to :class:`JwksFetchError`; malformed payloads map to
  :class:`JwksPayloadError`.
* When the TTL elapses the next lookup refreshes the cache automatically.
* The reconstructed RSA public key actually verifies a JWT signed with the
  matching private key — this is the contract Subagent C's CSP JWKS endpoint
  pins on the producer side.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from freezegun import freeze_time
from jose import jwt

from app.config import settings


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _int_to_base64url(value: int) -> str:
    """Mirror the encoding csp uses in app/api/jwks.py."""
    if value == 0:
        return "AA"
    byte_length = (value.bit_length() + 7) // 8
    raw = value.to_bytes(byte_length, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _generate_rsa_keypair() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks_payload_for(private_key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    pub = private_key.public_key()
    nums = pub.public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": kid,
                "n": _int_to_base64url(nums.n),
                "e": _int_to_base64url(nums.e),
            }
        ]
    }


def _multi_jwks_payload(items: list[tuple[rsa.RSAPrivateKey, str]]) -> dict[str, Any]:
    keys: list[dict[str, Any]] = []
    for priv, kid in items:
        nums = priv.public_key().public_numbers()
        keys.append(
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": kid,
                "n": _int_to_base64url(nums.n),
                "e": _int_to_base64url(nums.e),
            }
        )
    return {"keys": keys}


def _private_pem(private_key: rsa.RSAPrivateKey) -> bytes:
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_jwks_client_state():
    """Each test gets a fresh module-level singleton state."""
    from app.services import jwks_client as mod

    mod._reset_for_tests()
    yield
    mod._reset_for_tests()


@pytest.fixture
def fast_ttl(monkeypatch):
    """Most tests don't care about the refresh task — keep TTL at default but
    expose a knob for the TTL-refresh test."""
    monkeypatch.setattr(settings, "JWKS_REFRESH_SECONDS", 3600)
    yield


@pytest.fixture
def csp_base_url(monkeypatch) -> str:
    url = "http://csp-test:8000"
    monkeypatch.setattr(settings, "CSP_BASE_URL", url)
    return url


# ----------------------------------------------------------------------------
# 1. Cold-start fetch
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cold_start_fetches_jwks_and_returns_public_key(fast_ttl, csp_base_url):
    from app.services.jwks_client import JwksClient

    priv = _generate_rsa_keypair()
    payload = _jwks_payload_for(priv, kid="anila-v1")

    async with respx.mock(base_url=csp_base_url) as router:
        route = router.get("/.well-known/jwks.json").mock(
            return_value=httpx.Response(200, json=payload)
        )
        client = JwksClient()
        key = await client.get_public_key("anila-v1")

        assert isinstance(key, RSAPublicKey)
        assert route.call_count == 1
        actual_modulus = priv.public_key().public_numbers().n
        assert key.public_numbers().n == actual_modulus


# ----------------------------------------------------------------------------
# 2. Cache hit — second lookup does not re-issue HTTP
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_hit_does_not_refetch(fast_ttl, csp_base_url):
    from app.services.jwks_client import JwksClient

    priv = _generate_rsa_keypair()
    payload = _jwks_payload_for(priv, kid="anila-v1")

    async with respx.mock(base_url=csp_base_url) as router:
        route = router.get("/.well-known/jwks.json").mock(
            return_value=httpx.Response(200, json=payload)
        )
        client = JwksClient()
        first = await client.get_public_key("anila-v1")
        second = await client.get_public_key("anila-v1")

        assert first.public_numbers().n == second.public_numbers().n
        assert route.call_count == 1, "second lookup must hit cache"


# ----------------------------------------------------------------------------
# 3. Unknown kid forces one refetch; then raises if still missing
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_kid_triggers_single_refetch_then_raises(fast_ttl, csp_base_url):
    from app.services.jwks_client import JwksClient, JwksKeyNotFoundError

    priv = _generate_rsa_keypair()
    payload = _jwks_payload_for(priv, kid="anila-v1")

    async with respx.mock(base_url=csp_base_url) as router:
        route = router.get("/.well-known/jwks.json").mock(
            return_value=httpx.Response(200, json=payload)
        )
        client = JwksClient()

        # Prime cache
        await client.get_public_key("anila-v1")
        assert route.call_count == 1

        with pytest.raises(JwksKeyNotFoundError):
            await client.get_public_key("ghost-kid")

        # Exactly one extra fetch (the forced refresh on miss); not a loop
        assert route.call_count == 2, "missing kid must trigger exactly one refetch"


@pytest.mark.asyncio
async def test_repeated_unknown_kids_do_not_refetch_until_interval_elapses(
    fast_ttl, csp_base_url
):
    """未知 kid 會重抓一次讓輪替重疊生效，但同一間隔內不再打 CSP。"""
    from app.services.jwks_client import JwksClient, JwksKeyNotFoundError

    priv = _generate_rsa_keypair()
    payload = _jwks_payload_for(priv, kid="anila-v1")

    async with respx.mock(base_url=csp_base_url) as router:
        route = router.get("/.well-known/jwks.json").mock(
            return_value=httpx.Response(200, json=payload)
        )
        client = JwksClient()
        await client.get_public_key("anila-v1")
        with pytest.raises(JwksKeyNotFoundError):
            await client.get_public_key("ghost-a")
        assert route.call_count == 2

        with pytest.raises(JwksKeyNotFoundError, match="rate-limited"):
            await client.get_public_key("ghost-b")
        assert route.call_count == 2, "第二個未知 kid 不得再打 JWKS"


@pytest.mark.asyncio
async def test_unknown_kid_refetch_resumes_when_interval_is_zero(
    fast_ttl, csp_base_url, monkeypatch
):
    """間隔歸零後，未知 kid 可以再重抓，限制不是永久只抓一次。"""
    from app.config import settings
    from app.services.jwks_client import JwksClient, JwksKeyNotFoundError

    priv = _generate_rsa_keypair()
    payload = _jwks_payload_for(priv, kid="anila-v1")
    monkeypatch.setattr(settings, "JWKS_UNKNOWN_KID_REFETCH_SECONDS", 0)

    async with respx.mock(base_url=csp_base_url) as router:
        route = router.get("/.well-known/jwks.json").mock(
            return_value=httpx.Response(200, json=payload)
        )
        client = JwksClient()
        await client.get_public_key("anila-v1")
        with pytest.raises(JwksKeyNotFoundError):
            await client.get_public_key("ghost-a")
        with pytest.raises(JwksKeyNotFoundError):
            await client.get_public_key("ghost-b")
        assert route.call_count == 3


# ----------------------------------------------------------------------------
# 4. HTTP error on fetch → JwksFetchError
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_error_raises_jwks_fetch_error(fast_ttl, csp_base_url):
    from app.services.jwks_client import JwksClient, JwksFetchError

    async with respx.mock(base_url=csp_base_url) as router:
        router.get("/.well-known/jwks.json").mock(return_value=httpx.Response(503))
        client = JwksClient()
        with pytest.raises(JwksFetchError):
            await client.get_public_key("anila-v1")


@pytest.mark.asyncio
async def test_network_error_raises_jwks_fetch_error(fast_ttl, csp_base_url):
    from app.services.jwks_client import JwksClient, JwksFetchError

    async with respx.mock(base_url=csp_base_url) as router:
        router.get("/.well-known/jwks.json").mock(
            side_effect=httpx.ConnectError("boom")
        )
        client = JwksClient()
        with pytest.raises(JwksFetchError):
            await client.get_public_key("anila-v1")


# ----------------------------------------------------------------------------
# 5. Malformed payload → JwksPayloadError
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_payload_missing_keys_field_raises(fast_ttl, csp_base_url):
    from app.services.jwks_client import JwksClient, JwksPayloadError

    async with respx.mock(base_url=csp_base_url) as router:
        router.get("/.well-known/jwks.json").mock(
            return_value=httpx.Response(200, json={"not_keys": []})
        )
        client = JwksClient()
        with pytest.raises(JwksPayloadError):
            await client.get_public_key("anila-v1")


@pytest.mark.asyncio
async def test_payload_key_missing_n_raises(fast_ttl, csp_base_url):
    from app.services.jwks_client import JwksClient, JwksPayloadError

    bad = {
        "keys": [
            {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": "anila-v1", "e": "AQAB"}
        ]
    }
    async with respx.mock(base_url=csp_base_url) as router:
        router.get("/.well-known/jwks.json").mock(
            return_value=httpx.Response(200, json=bad)
        )
        client = JwksClient()
        with pytest.raises(JwksPayloadError):
            await client.get_public_key("anila-v1")


@pytest.mark.asyncio
async def test_payload_key_missing_e_raises(fast_ttl, csp_base_url):
    from app.services.jwks_client import JwksClient, JwksPayloadError

    priv = _generate_rsa_keypair()
    n_b64 = _int_to_base64url(priv.public_key().public_numbers().n)
    bad = {
        "keys": [
            {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": "anila-v1", "n": n_b64}
        ]
    }
    async with respx.mock(base_url=csp_base_url) as router:
        router.get("/.well-known/jwks.json").mock(
            return_value=httpx.Response(200, json=bad)
        )
        client = JwksClient()
        with pytest.raises(JwksPayloadError):
            await client.get_public_key("anila-v1")


@pytest.mark.asyncio
async def test_payload_non_rsa_key_raises(fast_ttl, csp_base_url):
    """Non-RSA `kty` is rejected — we only verify RS256 today."""
    from app.services.jwks_client import JwksClient, JwksPayloadError

    bad = {
        "keys": [
            {
                "kty": "EC",
                "use": "sig",
                "alg": "ES256",
                "kid": "anila-v1",
                "x": "AA",
                "y": "AA",
                "crv": "P-256",
            }
        ]
    }
    async with respx.mock(base_url=csp_base_url) as router:
        router.get("/.well-known/jwks.json").mock(
            return_value=httpx.Response(200, json=bad)
        )
        client = JwksClient()
        with pytest.raises(JwksPayloadError):
            await client.get_public_key("anila-v1")


# ----------------------------------------------------------------------------
# 6. TTL refresh — past the TTL window, next lookup re-fetches
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ttl_expiry_forces_refresh_on_next_lookup(monkeypatch, csp_base_url):
    """If wall-clock advances past the TTL between two lookups, the second
    lookup must re-issue HTTP even when the kid is still cached."""
    from app.services.jwks_client import JwksClient

    monkeypatch.setattr(settings, "JWKS_REFRESH_SECONDS", 60)  # 1 minute

    priv = _generate_rsa_keypair()
    payload = _jwks_payload_for(priv, kid="anila-v1")

    async with respx.mock(base_url=csp_base_url) as router:
        route = router.get("/.well-known/jwks.json").mock(
            return_value=httpx.Response(200, json=payload)
        )
        client = JwksClient()

        with freeze_time("2026-01-01T00:00:00Z") as frozen:
            await client.get_public_key("anila-v1")
            assert route.call_count == 1

            # Push past TTL
            frozen.tick(delta=dt.timedelta(seconds=61))
            await client.get_public_key("anila-v1")
            assert route.call_count == 2, "expired cache must refetch"


# ----------------------------------------------------------------------------
# 7. End-to-end JWT verify (the real contract anila-studio cares about)
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_to_end_jwt_verify(fast_ttl, csp_base_url):
    """Sign a JWT with the matching private key, fetch JWKS via the client,
    and jose.decode must succeed. This is the integration shape app.auth will
    use in Wave-2."""
    from app.services.jwks_client import JwksClient

    priv = _generate_rsa_keypair()
    kid = "anila-v1"
    payload = _jwks_payload_for(priv, kid=kid)

    token = jwt.encode(
        {"sub": "user-42"},
        _private_pem(priv).decode("utf-8"),
        algorithm="RS256",
        headers={"kid": kid},
    )

    async with respx.mock(base_url=csp_base_url) as router:
        router.get("/.well-known/jwks.json").mock(
            return_value=httpx.Response(200, json=payload)
        )
        client = JwksClient()
        public_key = await client.get_public_key(kid)

        # jose accepts a `cryptography` RSAPublicKey directly via PEM
        public_pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        decoded = jwt.decode(token, public_pem, algorithms=["RS256"])
        assert decoded["sub"] == "user-42"


# ----------------------------------------------------------------------------
# 8. Key rotation — refreshed JWKS publishes new kid alongside old
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rotation_new_kid_picked_up_on_miss(fast_ttl, csp_base_url):
    """After a rotation event the JWKS endpoint returns both keys; existing
    cached lookups for the old kid stay valid, a lookup for the new kid forces
    a refresh and finds the new key."""
    from app.services.jwks_client import JwksClient

    old_priv = _generate_rsa_keypair()
    new_priv = _generate_rsa_keypair()
    old_payload = _jwks_payload_for(old_priv, kid="anila-v1")
    rotated_payload = _multi_jwks_payload(
        [(old_priv, "anila-v1"), (new_priv, "anila-v2")]
    )

    async with respx.mock(base_url=csp_base_url) as router:
        responses = [
            httpx.Response(200, json=old_payload),
            httpx.Response(200, json=rotated_payload),
        ]
        call_index = {"i": 0}

        def _respond(request: httpx.Request) -> httpx.Response:
            i = call_index["i"]
            call_index["i"] += 1
            return responses[min(i, len(responses) - 1)]

        route = router.get("/.well-known/jwks.json").mock(side_effect=_respond)
        client = JwksClient()

        # Cold-start: only old key available
        first_old = await client.get_public_key("anila-v1")
        assert route.call_count == 1
        assert (
            first_old.public_numbers().n == old_priv.public_key().public_numbers().n
        )

        # Lookup for new kid → forces refetch
        new_key = await client.get_public_key("anila-v2")
        assert route.call_count == 2
        assert (
            new_key.public_numbers().n == new_priv.public_key().public_numbers().n
        )

        # Old kid still resolvable from the refreshed cache, no extra fetch
        again_old = await client.get_public_key("anila-v1")
        assert route.call_count == 2
        assert (
            again_old.public_numbers().n == old_priv.public_key().public_numbers().n
        )


# ----------------------------------------------------------------------------
# 9. Lifespan: start/stop wires + cancels background refresh task
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_and_stop_lifespan_cleanly(monkeypatch, csp_base_url):
    """start(app) eagerly warms the cache and launches the refresh task;
    stop(app) cancels it cleanly without raising."""
    from app.services.jwks_client import start, stop, get_public_key

    monkeypatch.setattr(settings, "JWKS_REFRESH_SECONDS", 3600)

    priv = _generate_rsa_keypair()
    payload = _jwks_payload_for(priv, kid="anila-v1")

    async with respx.mock(base_url=csp_base_url) as router:
        route = router.get("/.well-known/jwks.json").mock(
            return_value=httpx.Response(200, json=payload)
        )

        app = object()  # opaque — start/stop accept any FastAPI-like obj
        await start(app)

        # Cold fetch already happened in start()
        assert route.call_count >= 1
        warmed_calls = route.call_count

        key = await get_public_key("anila-v1")
        assert isinstance(key, RSAPublicKey)
        # No extra HTTP after start() because cache is fresh
        assert route.call_count == warmed_calls

        await stop(app)


# ----------------------------------------------------------------------------
# 10. Module-level get_public_key delegates to the singleton client
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_module_level_get_public_key_uses_singleton(fast_ttl, csp_base_url):
    from app.services import jwks_client as mod

    priv = _generate_rsa_keypair()
    payload = _jwks_payload_for(priv, kid="anila-v1")

    async with respx.mock(base_url=csp_base_url) as router:
        router.get("/.well-known/jwks.json").mock(
            return_value=httpx.Response(200, json=payload)
        )
        first = await mod.get_public_key("anila-v1")
        second = await mod.get_public_key("anila-v1")
        assert first.public_numbers().n == second.public_numbers().n


# ----------------------------------------------------------------------------
# 11. Defensive validation edge cases
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_payload_keys_is_not_a_list_raises(fast_ttl, csp_base_url):
    from app.services.jwks_client import JwksClient, JwksPayloadError

    async with respx.mock(base_url=csp_base_url) as router:
        router.get("/.well-known/jwks.json").mock(
            return_value=httpx.Response(200, json={"keys": "not-a-list"})
        )
        client = JwksClient()
        with pytest.raises(JwksPayloadError):
            await client.get_public_key("anila-v1")


@pytest.mark.asyncio
async def test_payload_keys_empty_array_raises(fast_ttl, csp_base_url):
    from app.services.jwks_client import JwksClient, JwksPayloadError

    async with respx.mock(base_url=csp_base_url) as router:
        router.get("/.well-known/jwks.json").mock(
            return_value=httpx.Response(200, json={"keys": []})
        )
        client = JwksClient()
        with pytest.raises(JwksPayloadError):
            await client.get_public_key("anila-v1")


@pytest.mark.asyncio
async def test_payload_entry_missing_kid_raises(fast_ttl, csp_base_url):
    from app.services.jwks_client import JwksClient, JwksPayloadError

    priv = _generate_rsa_keypair()
    nums = priv.public_key().public_numbers()
    bad = {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "n": _int_to_base64url(nums.n),
                "e": _int_to_base64url(nums.e),
            }
        ]
    }
    async with respx.mock(base_url=csp_base_url) as router:
        router.get("/.well-known/jwks.json").mock(
            return_value=httpx.Response(200, json=bad)
        )
        client = JwksClient()
        with pytest.raises(JwksPayloadError):
            await client.get_public_key("anila-v1")


@pytest.mark.asyncio
async def test_payload_non_dict_root_raises(fast_ttl, csp_base_url):
    from app.services.jwks_client import JwksClient, JwksPayloadError

    async with respx.mock(base_url=csp_base_url) as router:
        router.get("/.well-known/jwks.json").mock(
            return_value=httpx.Response(200, json=["not", "a", "dict"])
        )
        client = JwksClient()
        with pytest.raises(JwksPayloadError):
            await client.get_public_key("anila-v1")


@pytest.mark.asyncio
async def test_empty_kid_raises_key_not_found(fast_ttl, csp_base_url):
    from app.services.jwks_client import JwksClient, JwksKeyNotFoundError

    client = JwksClient()
    with pytest.raises(JwksKeyNotFoundError):
        await client.get_public_key("")


@pytest.mark.asyncio
async def test_non_string_n_value_raises(fast_ttl, csp_base_url):
    """If ``n`` is the wrong JSON type (e.g. a number) we must raise instead
    of crashing inside the base64 decoder."""
    from app.services.jwks_client import JwksClient, JwksPayloadError

    bad = {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": "anila-v1",
                "n": 12345,  # should be a base64url string
                "e": "AQAB",
            }
        ]
    }
    async with respx.mock(base_url=csp_base_url) as router:
        router.get("/.well-known/jwks.json").mock(
            return_value=httpx.Response(200, json=bad)
        )
        client = JwksClient()
        with pytest.raises(JwksPayloadError):
            await client.get_public_key("anila-v1")


@pytest.mark.asyncio
async def test_non_json_response_raises_fetch_error(fast_ttl, csp_base_url):
    from app.services.jwks_client import JwksClient, JwksFetchError

    async with respx.mock(base_url=csp_base_url) as router:
        router.get("/.well-known/jwks.json").mock(
            return_value=httpx.Response(
                200, text="not valid json", headers={"content-type": "text/plain"}
            )
        )
        client = JwksClient()
        with pytest.raises(JwksFetchError):
            await client.get_public_key("anila-v1")


@pytest.mark.asyncio
async def test_start_background_refresh_is_idempotent(monkeypatch, csp_base_url):
    """Calling start_background_refresh twice does not leak tasks."""
    from app.services.jwks_client import JwksClient

    monkeypatch.setattr(settings, "JWKS_REFRESH_SECONDS", 3600)
    priv = _generate_rsa_keypair()
    payload = _jwks_payload_for(priv, kid="anila-v1")

    async with respx.mock(base_url=csp_base_url) as router:
        router.get("/.well-known/jwks.json").mock(
            return_value=httpx.Response(200, json=payload)
        )
        client = JwksClient()
        await client.warm()
        await client.start_background_refresh()
        first_task = client._refresh_task
        await client.start_background_refresh()
        second_task = client._refresh_task
        assert first_task is second_task, "second start must reuse the task"
        await client.stop_background_refresh()


@pytest.mark.asyncio
async def test_stop_without_start_is_noop():
    """stop() must not raise if start() was never called."""
    from app.services.jwks_client import JwksClient

    client = JwksClient()
    await client.stop_background_refresh()  # should not raise


# ----------------------------------------------------------------------------
# 12. Concurrent cold-start de-dupes via lock
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_cold_start_fetches_only_once(fast_ttl, csp_base_url):
    """Multiple coroutines hitting an empty cache simultaneously must result
    in exactly one HTTP fetch — the asyncio.Lock prevents thundering herd."""
    from app.services.jwks_client import JwksClient

    priv = _generate_rsa_keypair()
    payload = _jwks_payload_for(priv, kid="anila-v1")

    async with respx.mock(base_url=csp_base_url) as router:
        route = router.get("/.well-known/jwks.json").mock(
            return_value=httpx.Response(200, json=payload)
        )
        client = JwksClient()
        results = await asyncio.gather(
            client.get_public_key("anila-v1"),
            client.get_public_key("anila-v1"),
            client.get_public_key("anila-v1"),
            client.get_public_key("anila-v1"),
        )
        assert all(isinstance(k, RSAPublicKey) for k in results)
        assert route.call_count == 1, "lock must dedupe concurrent cold-starts"


@pytest.mark.asyncio
async def test_concurrent_unknown_kids_share_one_refetch(fast_ttl, csp_base_url):
    """同時送達的未知 kid 只打一次 JWKS，其餘請求共用那次結果。"""
    from app.services.jwks_client import JwksClient, JwksKeyNotFoundError

    priv = _generate_rsa_keypair()
    payload = _jwks_payload_for(priv, kid="anila-v1")
    started = asyncio.Event()
    release = asyncio.Event()
    calls = {"n": 0}

    async def handler(_request):
        calls["n"] += 1
        if calls["n"] >= 2:
            started.set()
            await release.wait()
        return httpx.Response(200, json=payload)

    async with respx.mock(base_url=csp_base_url) as router:
        router.get("/.well-known/jwks.json").mock(side_effect=handler)
        client = JwksClient()
        await client.get_public_key("anila-v1")
        assert calls["n"] == 1

        async def _miss(kid: str):
            with pytest.raises(JwksKeyNotFoundError):
                await client.get_public_key(kid)

        first = asyncio.create_task(_miss("ghost-a"))
        await started.wait()
        second = asyncio.create_task(_miss("ghost-b"))
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.gather(first, second)
        assert calls["n"] == 2


@pytest.mark.asyncio
async def test_unknown_kid_refetch_failure_reserves_backoff(fast_ttl, csp_base_url):
    """未知 kid 的抓取失敗也要佔住間隔，下一發不得立刻再打。"""
    from app.services.jwks_client import JwksClient, JwksFetchError, JwksKeyNotFoundError

    priv = _generate_rsa_keypair()
    payload = _jwks_payload_for(priv, kid="anila-v1")
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=payload)
        return httpx.Response(503, text="down")

    async with respx.mock(base_url=csp_base_url) as router:
        router.get("/.well-known/jwks.json").mock(side_effect=handler)
        client = JwksClient()
        await client.get_public_key("anila-v1")
        with pytest.raises(JwksFetchError):
            await client.get_public_key("ghost-a")
        with pytest.raises(JwksKeyNotFoundError, match="rate-limited"):
            await client.get_public_key("ghost-b")
        assert calls["n"] == 2
