"""``get_active_flux_provider()`` — 執行期向 csp 拉 image-primary,熱生效。

取代 studio_render.get_flux_provider() 在啟動時「只看 env」的判斷:
csp 有設定的 image-primary 端點/model 優先於 env;csp 404/連線失敗/401/403
一律 fallback 到 env(FLUX_BACKEND_URL / FLUX_MODEL);兩者皆空才維持既有
「FLUX 停用」語意。端點或 model 變更時,下一次解析會重建
FluxImageProvider,cache key 含 model 所以不會吃到舊模型的圖
(見 app/services/flux_image_provider.py 的 _cache_key)。
"""
from __future__ import annotations

import base64
import importlib

import httpx
import pytest
import respx

from app.config import settings
from app.schemas.studio import ImageUseCase

_CSP = settings.CSP_BASE_URL.rstrip("/")
_PRIMARY_URL = f"{_CSP}/api/models/image-primary"

_PNG_A = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32 + b"A"
_PNG_B = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32 + b"B"


def _primary_payload(endpoint: str, name: str) -> dict:
    return {
        "id": 1,
        "name": name,
        "display_name": name,
        "model_type": "image",
        "endpoint_url": endpoint,
        "api_version": None,
        "health_status": "healthy",
    }


def _images_response(png: bytes) -> httpx.Response:
    return httpx.Response(
        200,
        json={"created": 1_720_000_000, "data": [{"b64_json": base64.b64encode(png).decode()}]},
    )


@pytest.fixture(autouse=True)
def _reset_modules():
    import app.services.flux_image_primary as primary_mod
    import app.services.studio_render as render_mod

    importlib.reload(primary_mod)
    importlib.reload(render_mod)
    yield
    importlib.reload(primary_mod)
    importlib.reload(render_mod)


# ── csp 優先於 env ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_csp_value_takes_priority_over_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FLUX_BACKEND_URL", "http://env-flux:8000")
    monkeypatch.setenv("FLUX_MODEL", "env-model")
    monkeypatch.setenv("FLUX_CACHE_DIR", str(tmp_path / "fc"))

    respx.get(_PRIMARY_URL).mock(
        return_value=httpx.Response(
            200, json=_primary_payload("http://csp-flux:9000", "csp-model")
        )
    )

    import app.services.studio_render as render_mod

    provider = await render_mod.get_active_flux_provider()
    assert provider is not None
    assert provider.flux_url == "http://csp-flux:9000"
    assert provider.model == "csp-model"


# ── 404 fallback env ─────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_csp_404_falls_back_to_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FLUX_BACKEND_URL", "http://env-flux:8000")
    monkeypatch.setenv("FLUX_MODEL", "env-model")
    monkeypatch.setenv("FLUX_CACHE_DIR", str(tmp_path / "fc"))

    respx.get(_PRIMARY_URL).mock(return_value=httpx.Response(404, json={"detail": "unset"}))

    import app.services.studio_render as render_mod

    provider = await render_mod.get_active_flux_provider()
    assert provider is not None
    assert provider.flux_url == "http://env-flux:8000"
    assert provider.model == "env-model"


# ── csp 有設定 + env 空 → FLUX 可用(關鍵行為變更:不再是啟動時判斷)──


@pytest.mark.asyncio
@respx.mock
async def test_csp_configured_env_empty_still_enabled(monkeypatch, tmp_path):
    monkeypatch.delenv("FLUX_BACKEND_URL", raising=False)
    monkeypatch.delenv("FLUX_MODEL", raising=False)
    monkeypatch.setenv("FLUX_CACHE_DIR", str(tmp_path / "fc"))

    respx.get(_PRIMARY_URL).mock(
        return_value=httpx.Response(
            200, json=_primary_payload("http://csp-flux:9000", "csp-model")
        )
    )

    import app.services.studio_render as render_mod

    provider = await render_mod.get_active_flux_provider()
    assert provider is not None
    assert provider.flux_url == "http://csp-flux:9000"
    assert provider.model == "csp-model"


# ── 兩者皆空 → 停用語意不變 ───────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_both_empty_stays_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("FLUX_BACKEND_URL", raising=False)
    monkeypatch.delenv("FLUX_MODEL", raising=False)
    monkeypatch.setenv("FLUX_CACHE_DIR", str(tmp_path / "fc"))

    respx.get(_PRIMARY_URL).mock(return_value=httpx.Response(404, json={"detail": "unset"}))

    import app.services.studio_render as render_mod

    provider = await render_mod.get_active_flux_provider()
    assert provider is None


# ── 端點熱切換:下一次解析打新 URL ────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_endpoint_hot_switch_hits_new_url(monkeypatch, tmp_path):
    monkeypatch.delenv("FLUX_BACKEND_URL", raising=False)
    monkeypatch.setenv("FLUX_CACHE_DIR", str(tmp_path / "fc"))

    respx.get(_PRIMARY_URL).mock(
        return_value=httpx.Response(
            200, json=_primary_payload("http://flux-a.example:8000", "model-a")
        )
    )
    route_a = respx.post("http://flux-a.example:8000/v1/images/generations").mock(
        return_value=_images_response(_PNG_A)
    )
    route_b = respx.post("http://flux-b.example:8000/v1/images/generations").mock(
        return_value=_images_response(_PNG_B)
    )

    import app.services.flux_image_primary as primary_mod
    import app.services.studio_render as render_mod

    provider_a = await render_mod.get_active_flux_provider()
    out_a = await provider_a.get_or_generate("hero", use_case=ImageUseCase.COVER_HERO, seed=1)
    assert route_a.called
    assert not route_b.called
    assert out_a[0].png_bytes == _PNG_A

    # TTL 到期 + csp 端點換成 B。
    primary_mod._state["fetched_at"] -= primary_mod.IMAGE_PRIMARY_TTL_SECONDS + 1
    respx.get(_PRIMARY_URL).mock(
        return_value=httpx.Response(
            200, json=_primary_payload("http://flux-b.example:8000", "model-a")
        )
    )

    provider_b = await render_mod.get_active_flux_provider()
    assert provider_b.flux_url == "http://flux-b.example:8000"
    # 換一個 seed(不同 cache key)——同一個 prompt/seed 本來就該吃快取
    # (model 沒變,內容定址快取命中是設計上的省算力行為);這裡要驗證的是
    # 「快取沒命中時,下一次生圖真的打新端點」,所以刻意讓它 cache miss。
    out_b = await provider_b.get_or_generate("hero", use_case=ImageUseCase.COVER_HERO, seed=2)
    assert route_b.called
    assert out_b[0].png_bytes == _PNG_B


# ── model 切換後 cache 不回舊模型的圖 ────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_model_switch_does_not_reuse_stale_cached_image(monkeypatch, tmp_path):
    monkeypatch.delenv("FLUX_BACKEND_URL", raising=False)
    # 同一個 cache_dir 貫穿整個測試 —— 模擬 provider 重建但快取磁碟共用。
    monkeypatch.setenv("FLUX_CACHE_DIR", str(tmp_path / "fc"))

    respx.get(_PRIMARY_URL).mock(
        return_value=httpx.Response(
            200, json=_primary_payload("http://flux-same:8000", "model-a")
        )
    )
    route = respx.post("http://flux-same:8000/v1/images/generations").mock(
        side_effect=[_images_response(_PNG_A), _images_response(_PNG_B)]
    )

    import app.services.flux_image_primary as primary_mod
    import app.services.studio_render as render_mod

    provider_1 = await render_mod.get_active_flux_provider()
    out_1 = await provider_1.get_or_generate(
        "same prompt", use_case=ImageUseCase.COVER_HERO, seed=42, style_id="default"
    )
    assert out_1[0].png_bytes == _PNG_A
    assert route.call_count == 1

    # 端點不變,但 csp 把 model 換成 model-b(同一台服務換掛的模型)。
    primary_mod._state["fetched_at"] -= primary_mod.IMAGE_PRIMARY_TTL_SECONDS + 1
    respx.get(_PRIMARY_URL).mock(
        return_value=httpx.Response(
            200, json=_primary_payload("http://flux-same:8000", "model-b")
        )
    )

    provider_2 = await render_mod.get_active_flux_provider()
    assert provider_2.model == "model-b"
    out_2 = await provider_2.get_or_generate(
        "same prompt", use_case=ImageUseCase.COVER_HERO, seed=42, style_id="default"
    )
    # 沒有把 model 加進 cache key 的話,這裡會直接命中 model-a 留下的快取檔案
    # 拿到 _PNG_A —— 這正是 spec 要求要避免的「切模型吃到舊圖」。
    assert out_2[0].png_bytes == _PNG_B
    assert route.call_count == 2
