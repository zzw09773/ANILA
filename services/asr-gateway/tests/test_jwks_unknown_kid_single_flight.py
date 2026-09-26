"""未知 kid 重抓要在鎖內單飛，失敗也要退避。"""
from __future__ import annotations

import asyncio
import base64

import httpx
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import settings


def _b64url_int(value: int) -> str:
    length = (value.bit_length() + 7) // 8 or 1
    raw = value.to_bytes(length, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _payload(private_key, kid: str) -> dict:
    numbers = private_key.public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": kid,
                "n": _b64url_int(numbers.n),
                "e": _b64url_int(numbers.e),
            }
        ]
    }


@pytest.fixture
def csp_base_url(monkeypatch) -> str:
    url = "http://csp-test:8000"
    monkeypatch.setattr(settings, "CSP_BASE_URL", url)
    monkeypatch.setattr(settings, "JWKS_REFRESH_SECONDS", 3600)
    return url


async def test_unknown_kid_refetch_failure_reserves_backoff(csp_base_url):
    from app.services.jwks_client import JwksClient, JwksFetchError, JwksKeyNotFoundError

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    payload = _payload(private_key, "anila-v1")
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


async def test_concurrent_unknown_kids_share_one_refetch(csp_base_url):
    from app.services.jwks_client import JwksClient, JwksKeyNotFoundError

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    payload = _payload(private_key, "anila-v1")
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
