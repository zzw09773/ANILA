"""未知 kid 的 JWKS 重抓有上限，輪替重疊仍抓得到新鑰。"""
from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from anila_core.api.middleware.jwks_client import (
    JwksClient,
    JwksKeyNotFoundError,
    parse_jwks,
)


def _b64url_int(value: int) -> str:
    length = (value.bit_length() + 7) // 8 or 1
    raw = value.to_bytes(length, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


_NUMBERS = rsa.generate_private_key(
    public_exponent=65537, key_size=2048
).public_key().public_numbers()


def _doc(*kids: str) -> dict:
    keys = []
    for kid in kids:
        keys.append(
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": kid,
                "n": _b64url_int(_NUMBERS.n),
                "e": _b64url_int(_NUMBERS.e),
            }
        )
    return {"keys": keys}


@pytest.mark.asyncio
async def test_unknown_kid_refetches_once_then_rate_limits():
    state = {"doc": _doc("active")}
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        return parse_jwks(state["doc"])

    client = JwksClient(
        "https://csp.test/.well-known/jwks.json",
        fetch_fn=fetch,
    )
    await client.get_public_key("active")
    assert calls["n"] == 1

    with pytest.raises(JwksKeyNotFoundError):
        await client.get_public_key("ghost-a")
    assert calls["n"] == 2

    with pytest.raises(JwksKeyNotFoundError, match="rate-limited"):
        await client.get_public_key("ghost-b")
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_unknown_kid_refetch_finds_key_published_during_overlap():
    state = {"doc": _doc("active")}

    async def fetch():
        return parse_jwks(state["doc"])

    client = JwksClient(
        "https://csp.test/.well-known/jwks.json",
        fetch_fn=fetch,
    )
    await client.get_public_key("active")
    state["doc"] = _doc("active", "next")
    key = await client.get_public_key("next")
    assert key.public_numbers().e == 65537


@pytest.mark.asyncio
async def test_zero_interval_allows_another_unknown_kid_refetch():
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        return parse_jwks(_doc("active"))

    client = JwksClient(
        "https://csp.test/.well-known/jwks.json",
        fetch_fn=fetch,
        unknown_kid_refetch_interval=0,
    )
    await client.get_public_key("active")
    with pytest.raises(JwksKeyNotFoundError):
        await client.get_public_key("ghost-a")
    with pytest.raises(JwksKeyNotFoundError):
        await client.get_public_key("ghost-b")
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_concurrent_unknown_kids_share_one_refetch():
    import asyncio

    calls = {"n": 0}
    started = asyncio.Event()
    release = asyncio.Event()

    async def fetch():
        calls["n"] += 1
        if calls["n"] >= 2:
            started.set()
            await release.wait()
        return parse_jwks(_doc("active"))

    client = JwksClient(
        "https://csp.test/.well-known/jwks.json",
        fetch_fn=fetch,
    )
    await client.get_public_key("active")

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
async def test_unknown_kid_refetch_failure_reserves_backoff():
    from anila_core.api.middleware.jwks_client import JwksFetchError

    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        if calls["n"] == 1:
            return parse_jwks(_doc("active"))
        raise JwksFetchError("down")

    client = JwksClient(
        "https://csp.test/.well-known/jwks.json",
        fetch_fn=fetch,
    )
    await client.get_public_key("active")
    with pytest.raises(JwksFetchError):
        await client.get_public_key("ghost-a")
    with pytest.raises(JwksKeyNotFoundError, match="rate-limited"):
        await client.get_public_key("ghost-b")
    assert calls["n"] == 2
