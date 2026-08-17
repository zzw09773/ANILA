"""FLUX image-primary fetcher — 60s TTL 快取 + 錯誤處理表驗證。

比照 services/anila-core-router/main.py 的 ``_refresh_primary`` /
``_ensure_primary`` 結構:studio 執行期向 csp 拉 image-primary(admin 在
ModelsView 標記的主圖像模型),60 秒內不重打 csp;404/連線失敗/401/403
的行為見 docs/specs/specs/2026-07-06-flux-image-primary-design.md
的錯誤處理表。

認證沿用 studio 既有的 service-token 機制(``settings.CSP_SERVICE_TOKEN``
→ header ``X-CSP-Service-Token``,和 job_reporting.py / revocation_cache.py
同一顆設定值),不是新 env。
"""
from __future__ import annotations

import importlib
import logging

import httpx
import pytest
import respx

from app.config import settings

_CSP = settings.CSP_BASE_URL.rstrip("/")
_URL = f"{_CSP}/api/models/image-primary"


@pytest.fixture(autouse=True)
def _reset_module():
    import app.services.flux_image_primary as mod

    importlib.reload(mod)
    yield
    importlib.reload(mod)


def _ok_payload(endpoint: str = "https://gw.example/v1", name: str = "flux.2-pro") -> dict:
    return {
        "id": 1,
        "name": name,
        "display_name": "FLUX Pro",
        "model_type": "image",
        "endpoint_url": endpoint,
        "api_version": None,
        "health_status": "healthy",
    }


# ── 200 → (endpoint, model) ─────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_returns_endpoint_and_model_on_200():
    route = respx.get(_URL).mock(return_value=httpx.Response(200, json=_ok_payload()))
    import app.services.flux_image_primary as mod

    endpoint, model = await mod.get_image_primary()
    assert route.called
    assert endpoint == "https://gw.example/v1"
    assert model == "flux.2-pro"


# ── TTL:60 秒內不重打 csp ────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_ttl_60s_no_refetch():
    route = respx.get(_URL).mock(return_value=httpx.Response(200, json=_ok_payload()))
    import app.services.flux_image_primary as mod

    await mod.get_image_primary()
    await mod.get_image_primary()
    await mod.get_image_primary()
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_ttl_expiry_triggers_refetch():
    route = respx.get(_URL).mock(return_value=httpx.Response(200, json=_ok_payload()))
    import app.services.flux_image_primary as mod

    await mod.get_image_primary()
    mod._state["fetched_at"] -= mod.IMAGE_PRIMARY_TTL_SECONDS + 1
    await mod.get_image_primary()
    assert route.call_count == 2


# ── 404 → fallback env(回 None,None,由呼叫端決定 fallback);log 一次 ──


@pytest.mark.asyncio
@respx.mock
async def test_404_falls_back_and_logs_once(caplog):
    route = respx.get(_URL).mock(return_value=httpx.Response(404, json={"detail": "not set"}))
    import app.services.flux_image_primary as mod

    caplog.set_level(logging.INFO, logger="app.services.flux_image_primary")

    endpoint, model = await mod.get_image_primary()
    assert (endpoint, model) == (None, None)

    # 第二次(TTL 到期後)還是 404 —— 不應該再 log 一次(別每 60 秒刷屏)。
    mod._state["fetched_at"] -= mod.IMAGE_PRIMARY_TTL_SECONDS + 1
    endpoint2, model2 = await mod.get_image_primary()
    assert (endpoint2, model2) == (None, None)
    assert route.call_count == 2

    not_found_logs = [r for r in caplog.records if "尚未設定" in r.getMessage()]
    assert len(not_found_logs) == 1, "404 只能 log 一次,不能每次 TTL 到期都刷屏"


@pytest.mark.asyncio
@respx.mock
async def test_409_disabled_treated_like_404():
    respx.get(_URL).mock(return_value=httpx.Response(409, json={"detail": "disabled"}))
    import app.services.flux_image_primary as mod

    endpoint, model = await mod.get_image_primary()
    assert (endpoint, model) == (None, None)


# ── 連線失敗:沿用上次快取值;沒有快取 → fallback env ──────────────────


@pytest.mark.asyncio
@respx.mock
async def test_connection_failure_keeps_last_cached_value():
    import app.services.flux_image_primary as mod

    respx.get(_URL).mock(return_value=httpx.Response(200, json=_ok_payload()))
    endpoint1, model1 = await mod.get_image_primary()
    assert endpoint1 == "https://gw.example/v1"

    mod._state["fetched_at"] -= mod.IMAGE_PRIMARY_TTL_SECONDS + 1
    respx.get(_URL).mock(side_effect=httpx.ConnectError("boom"))
    endpoint2, model2 = await mod.get_image_primary()
    assert (endpoint2, model2) == (endpoint1, model1)


@pytest.mark.asyncio
@respx.mock
async def test_connection_failure_with_no_prior_cache_returns_none():
    respx.get(_URL).mock(side_effect=httpx.ConnectError("boom"))
    import app.services.flux_image_primary as mod

    endpoint, model = await mod.get_image_primary()
    assert (endpoint, model) == (None, None)


# ── service token 無效(401/403)→ fallback env + warning ────────────────


@pytest.mark.asyncio
@respx.mock
async def test_401_falls_back_and_warns(caplog):
    respx.get(_URL).mock(return_value=httpx.Response(401, json={"detail": "bad token"}))
    import app.services.flux_image_primary as mod

    caplog.set_level(logging.WARNING, logger="app.services.flux_image_primary")
    endpoint, model = await mod.get_image_primary()
    assert (endpoint, model) == (None, None)
    assert any("401" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@respx.mock
async def test_403_falls_back():
    respx.get(_URL).mock(return_value=httpx.Response(403, json={"detail": "forbidden"}))
    import app.services.flux_image_primary as mod

    endpoint, model = await mod.get_image_primary()
    assert (endpoint, model) == (None, None)


# ── service token header:重用既有 settings.CSP_SERVICE_TOKEN ───────────


@pytest.mark.asyncio
@respx.mock
async def test_service_token_header_sent_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "csk-test-token")
    route = respx.get(_URL).mock(return_value=httpx.Response(200, json=_ok_payload()))
    import app.services.flux_image_primary as mod

    await mod.get_image_primary()
    assert route.calls.last.request.headers["x-csp-service-token"] == "csk-test-token"


@pytest.mark.asyncio
@respx.mock
async def test_no_service_token_header_when_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "")
    route = respx.get(_URL).mock(return_value=httpx.Response(200, json=_ok_payload()))
    import app.services.flux_image_primary as mod

    await mod.get_image_primary()
    assert "x-csp-service-token" not in route.calls.last.request.headers
