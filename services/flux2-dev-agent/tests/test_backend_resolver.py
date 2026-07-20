"""BackendResolver: 組合 image-primary fetcher + env fallback,決定
每次生圖要打的 (endpoint_url, model)。CSP 有值時優先於 env;都沒有
時丟 FluxBackendUnconfigured 讓呼叫端回明確錯誤。
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.backend_resolver import BackendResolver, FluxBackendUnconfigured


@pytest.mark.asyncio
async def test_csp_value_wins_over_env_fallback():
    fetcher = AsyncMock()
    fetcher.get.return_value = ("https://flux-a.example.com", "flux-cloud-a")
    resolver = BackendResolver(
        fetcher=fetcher,
        fallback_endpoint="http://flux2-dev:8000",
        fallback_model="flux.2-dev",
    )
    assert await resolver.resolve() == ("https://flux-a.example.com", "flux-cloud-a")


@pytest.mark.asyncio
async def test_falls_back_to_env_when_fetcher_has_no_value():
    fetcher = AsyncMock()
    fetcher.get.return_value = (None, None)
    resolver = BackendResolver(
        fetcher=fetcher,
        fallback_endpoint="http://flux2-dev:8000",
        fallback_model="flux.2-dev",
    )
    assert await resolver.resolve() == ("http://flux2-dev:8000", "flux.2-dev")


@pytest.mark.asyncio
async def test_no_fetcher_uses_env_directly():
    resolver = BackendResolver(
        fetcher=None, fallback_endpoint="http://x:8000", fallback_model="m"
    )
    assert await resolver.resolve() == ("http://x:8000", "m")


@pytest.mark.asyncio
async def test_raises_clear_error_when_nothing_configured():
    fetcher = AsyncMock()
    fetcher.get.return_value = (None, None)
    resolver = BackendResolver(fetcher=fetcher, fallback_endpoint="", fallback_model="")
    with pytest.raises(FluxBackendUnconfigured):
        await resolver.resolve()


@pytest.mark.asyncio
async def test_partial_fallback_config_still_raises():
    """endpoint 有值但 model 空(或反之)視同未設定——不能半吊子上線。"""
    fetcher = AsyncMock()
    fetcher.get.return_value = (None, None)
    resolver = BackendResolver(
        fetcher=fetcher, fallback_endpoint="http://x:8000", fallback_model=""
    )
    with pytest.raises(FluxBackendUnconfigured):
        await resolver.resolve()


@pytest.mark.asyncio
async def test_formal_governance_never_uses_env_when_authority_missing():
    fetcher = AsyncMock()
    fetcher.get.return_value = (None, None)
    resolver = BackendResolver(
        fetcher=fetcher,
        fallback_endpoint="http://env-flux:8000",
        fallback_model="env-model",
        governance_required=True,
    )

    with pytest.raises(FluxBackendUnconfigured, match="formal model governance"):
        await resolver.resolve()
