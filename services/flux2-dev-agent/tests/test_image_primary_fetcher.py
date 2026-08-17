"""ImagePrimaryFetcher: 60s TTL 快取 + asyncio.Lock 的 image-primary 讀取。

比照 services/anila-core-router/main.py 的 _refresh_primary 結構;
行為見 docs/specs/2026-07-06-flux-image-primary-design.md
§3 與錯誤處理表。
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from app.image_primary_fetcher import ImagePrimaryFetcher

_URL = "http://csp:8000/api/models/image-primary"
_OK_BODY = {
    "id": 1,
    "name": "flux-cloud-a",
    "display_name": "FLUX Cloud A",
    "model_type": "image",
    "endpoint_url": "https://flux-a.example.com",
    "api_version": None,
    "health_status": "healthy",
}
_OK_BODY_2 = {**_OK_BODY, "name": "flux-cloud-b", "endpoint_url": "https://flux-b.example.com"}


def _fetcher(**kw) -> ImagePrimaryFetcher:
    kw.setdefault("csp_base_url", "http://csp:8000")
    kw.setdefault("service_token", "svc-token")
    return ImagePrimaryFetcher(**kw)


@pytest.mark.asyncio
@respx.mock
async def test_get_returns_endpoint_and_model_on_200():
    respx.get(_URL).mock(return_value=httpx.Response(200, json=_OK_BODY))
    fetcher = _fetcher()
    endpoint, model = await fetcher.get()
    assert endpoint == "https://flux-a.example.com"
    assert model == "flux-cloud-a"


@pytest.mark.asyncio
@respx.mock
async def test_sends_service_token_header():
    route = respx.get(_URL).mock(return_value=httpx.Response(200, json=_OK_BODY))
    fetcher = _fetcher(service_token="secret-token")
    await fetcher.get()
    assert route.calls.last.request.headers["x-csp-service-token"] == "secret-token"


@pytest.mark.asyncio
@respx.mock
async def test_no_service_token_sends_no_header():
    route = respx.get(_URL).mock(return_value=httpx.Response(200, json=_OK_BODY))
    fetcher = _fetcher(service_token="")
    await fetcher.get()
    assert "x-csp-service-token" not in route.calls.last.request.headers


@pytest.mark.asyncio
@respx.mock
async def test_ttl_caches_within_window_no_second_call():
    route = respx.get(_URL).mock(return_value=httpx.Response(200, json=_OK_BODY))
    fetcher = _fetcher(ttl_seconds=60.0)
    await fetcher.get()
    await fetcher.get()
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_ttl_expiry_refetches_and_picks_up_new_endpoint():
    route = respx.get(_URL).mock(
        side_effect=[
            httpx.Response(200, json=_OK_BODY),
            httpx.Response(200, json=_OK_BODY_2),
        ]
    )
    fetcher = _fetcher(ttl_seconds=0.05)
    first = await fetcher.get()
    await asyncio.sleep(0.1)
    second = await fetcher.get()

    assert route.call_count == 2
    assert first == ("https://flux-a.example.com", "flux-cloud-a")
    assert second == ("https://flux-b.example.com", "flux-cloud-b")


@pytest.mark.asyncio
@respx.mock
async def test_404_clears_cache_and_returns_none():
    respx.get(_URL).mock(return_value=httpx.Response(404, json={"detail": "尚未指定"}))
    fetcher = _fetcher()
    endpoint, model = await fetcher.get()
    assert (endpoint, model) == (None, None)


@pytest.mark.asyncio
@respx.mock
async def test_409_disabled_treated_like_404():
    respx.get(_URL).mock(return_value=httpx.Response(409, json={"detail": "已停用"}))
    fetcher = _fetcher()
    endpoint, model = await fetcher.get()
    assert (endpoint, model) == (None, None)


@pytest.mark.asyncio
@respx.mock
async def test_connection_error_keeps_last_cached_value():
    respx.get(_URL).mock(
        side_effect=[httpx.Response(200, json=_OK_BODY), httpx.ConnectError("boom")]
    )
    fetcher = _fetcher(ttl_seconds=0.05)
    first = await fetcher.get()
    await asyncio.sleep(0.1)
    second = await fetcher.get()
    assert first == second == ("https://flux-a.example.com", "flux-cloud-a")


@pytest.mark.asyncio
@respx.mock
async def test_401_clears_cache_falls_back_to_env():
    """spec 錯誤處理表：401/403 且無 rotating token 機制 → fallback env。
    舊快取必須清掉，否則 token 被撤銷後會永遠沿用最後一次的 CSP 值
    （Codex 審查發現）。"""
    respx.get(_URL).mock(
        side_effect=[httpx.Response(200, json=_OK_BODY), httpx.Response(401, json={})]
    )
    fetcher = _fetcher(ttl_seconds=0.05)
    first = await fetcher.get()
    assert first == ("https://flux-a.example.com", "flux-cloud-a")
    await asyncio.sleep(0.1)
    endpoint, model = await fetcher.get()
    assert (endpoint, model) == (None, None)


@pytest.mark.asyncio
@respx.mock
async def test_401_with_no_prior_cache_returns_none_not_raise():
    respx.get(_URL).mock(return_value=httpx.Response(401, json={"detail": "無效"}))
    fetcher = _fetcher()
    endpoint, model = await fetcher.get()
    assert (endpoint, model) == (None, None)


@pytest.mark.asyncio
@respx.mock
async def test_malformed_200_body_returns_none():
    respx.get(_URL).mock(return_value=httpx.Response(200, json={"id": 1}))
    fetcher = _fetcher()
    endpoint, model = await fetcher.get()
    assert (endpoint, model) == (None, None)
