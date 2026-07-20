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
import json
import sys
from types import ModuleType

import httpx
import pytest
import respx

from app.config import settings
from app.schemas.studio import ImageUseCase
from app.services.runtime_context import StudioRuntimeContext, use_runtime_context

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


def _runtime_context() -> StudioRuntimeContext:
    return StudioRuntimeContext(
        job_id="studio-formal-image-job",
        artifact_type="slides",
        requester_user_id=7,
        requester_employee_id="EMP0007",
        collection_id=11,
        task_id=13,
        source_snapshot_id=17,
        trace_id="studio-formal-image-trace",
        attempt=1,
        lease_token="lease-token-123456789",
    )


def _enable_formal_runtime(monkeypatch) -> None:
    monkeypatch.setattr(settings, "GATE5_MODEL_GOVERNANCE_ENABLED", True)
    monkeypatch.setattr(settings, "STUDIO_RUNTIME_SERVICE_TOKEN", "csk-studio-runtime")
    monkeypatch.setattr(settings, "STUDIO_ARTIFACT_SERVICE_TOKEN", "csk-studio-artifact")


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


@pytest.mark.asyncio
@respx.mock
async def test_formal_authority_revoke_disables_provider_before_second_downstream_call(
    monkeypatch, tmp_path
):
    """Formal mode bypasses TTL and must not call a pinned FLUX endpoint after revoke."""
    _enable_formal_runtime(monkeypatch)
    monkeypatch.delenv("FLUX_BACKEND_URL", raising=False)
    monkeypatch.setenv("FLUX_CACHE_DIR", str(tmp_path / "fc"))

    respx.get(_PRIMARY_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json=_primary_payload("http://flux-governed:8000", "model-a"),
            ),
            httpx.Response(503, json={"detail": "provider authority revoked"}),
        ]
    )
    downstream = respx.post(
        f"{_CSP}/v1/studio-runtime/images/generations"
    ).mock(return_value=_images_response(_PNG_A))

    import app.services.flux_image_primary as primary_mod
    import app.services.studio_render as render_mod

    with use_runtime_context(_runtime_context()):
        provider = await render_mod.get_active_flux_provider()
        assert provider is not None
        assert provider.via_csp_runtime is True
        await provider.get_or_generate(
            "governed prompt", use_case=ImageUseCase.COVER_HERO, seed=99
        )
    assert downstream.call_count == 1

    with use_runtime_context(_runtime_context()):
        revoked_provider = await render_mod.get_active_flux_provider()
    assert revoked_provider is None
    assert downstream.call_count == 1
    # Formal mode did not rely on the TTL; the second get above reached CSP.
    assert primary_mod._state["endpoint"] is None


@pytest.mark.asyncio
@respx.mock
async def test_formal_multi_image_deck_rechecks_before_each_cache_miss(
    monkeypatch, tmp_path
):
    """Revoke between slides: the second image must not hit the old FLUX URL."""
    _enable_formal_runtime(monkeypatch)
    monkeypatch.setenv("FLUX_CACHE_DIR", str(tmp_path / "fc"))

    respx.get(_PRIMARY_URL).mock(
        side_effect=[
            # Initial render setup, then the first image's preflight.
            httpx.Response(
                200,
                json=_primary_payload("http://flux-governed:8000", "model-a"),
            ),
            httpx.Response(
                200,
                json=_primary_payload("http://flux-governed:8000", "model-a"),
            ),
            # The second slide is a new cache key and must fail closed here.
            httpx.Response(503, json={"detail": "provider authority revoked"}),
        ]
    )
    downstream = respx.post(
        f"{_CSP}/v1/studio-runtime/images/generations"
    ).mock(return_value=_images_response(_PNG_A))

    import app.services.studio_render as render_mod
    with use_runtime_context(_runtime_context()):
        initial_provider = await render_mod.get_active_flux_provider()
        assert initial_provider is not None

    async def rewrite(**_kwargs):
        return "governed scene, no text"

    async def accept_first(candidates, **_kwargs):
        candidate = candidates[0]
        candidate.accepted = True
        candidate.vlm_verdict = {"match": True, "has_text": False, "score": 1.0}
        return candidate

    class _Llm:
        _bearer = "test-bearer"

        async def complete(self, **_kwargs):
            return "unused"

    monkeypatch.setattr("app.services.flux_prompt_rewriter.derive_flux_prompt", rewrite)
    monkeypatch.setattr("app.services.flux_quality_gate.gate_candidates", accept_first)

    # This deck has no diagram slide.  Isolate the optional Graphviz helper so
    # the image-governance test does not require its unrelated OpenCC import.
    diagram_renderer = ModuleType("app.services.diagram_renderer")

    async def render_dot_to_png(_dot):
        return None

    diagram_renderer.render_dot_to_png = render_dot_to_png
    monkeypatch.setitem(sys.modules, "app.services.diagram_renderer", diagram_renderer)

    with use_runtime_context(_runtime_context()):
        result = await render_mod._hydrate_images(
            {
                "slides": [
                    {"title": "Cover", "bullets": ["a"], "layout_kind": "cover"},
                    {
                        "title": "Content",
                        "bullets": ["b"],
                        "layout_kind": "standard",
                        "image_prompt": "illustration",
                    },
                ]
            },
            {},
            bearer="test-bearer",
            flux_provider=initial_provider,
            deck_base_seed=1000,
            llm=_Llm(),
        )

    assert downstream.call_count == 1
    assert "image_data" in result["slides"][0]
    assert "image_data" not in result["slides"][1]
    assert result["slides"][1]["image_gen_meta"]["fallback"] == "text_only"


@pytest.mark.asyncio
@respx.mock
async def test_formal_image_transport_uses_only_csp_runtime_headers(
    monkeypatch, tmp_path
):
    _enable_formal_runtime(monkeypatch)
    monkeypatch.setenv("FLUX_BACKEND_URL", "http://raw-flux-forbidden:8000")
    monkeypatch.setenv("FLUX_CACHE_DIR", str(tmp_path / "fc"))
    respx.get(_PRIMARY_URL).mock(
        return_value=httpx.Response(
            200,
            json=_primary_payload("http://raw-flux-forbidden:8000", "formal-image"),
        )
    )
    governed = respx.post(
        f"{_CSP}/v1/studio-runtime/images/generations"
    ).mock(return_value=_images_response(_PNG_A))
    raw = respx.post("http://raw-flux-forbidden:8000/v1/images/generations").mock(
        return_value=_images_response(_PNG_B)
    )

    import app.services.studio_render as render_mod

    with use_runtime_context(_runtime_context()):
        provider = await render_mod.get_active_flux_provider()
        assert provider is not None and provider.via_csp_runtime is True
        images = await provider.get_or_generate(
            "formal prompt", use_case=ImageUseCase.COVER_HERO, seed=3
        )

    assert images[0].png_bytes == _PNG_A
    assert governed.call_count == 1
    assert raw.call_count == 0
    request = governed.calls.last.request
    assert request.headers["x-csp-service-token"] == "csk-studio-runtime"
    assert request.headers["x-anila-task-id"] == "13"
    assert request.headers["x-studio-lease-token"] == "lease-token-123456789"
    assert json.loads(request.content)["model"] == "formal-image"


@pytest.mark.asyncio
@respx.mock
async def test_formal_image_requires_runtime_context_and_valid_runtime_token(
    monkeypatch, tmp_path
):
    _enable_formal_runtime(monkeypatch)
    monkeypatch.setenv("FLUX_BACKEND_URL", "http://raw-flux-forbidden:8000")
    monkeypatch.setenv("FLUX_CACHE_DIR", str(tmp_path / "fc"))
    respx.get(_PRIMARY_URL).mock(
        return_value=httpx.Response(
            200,
            json=_primary_payload("http://raw-flux-forbidden:8000", "formal-image"),
        )
    )
    governed = respx.post(
        f"{_CSP}/v1/studio-runtime/images/generations"
    ).mock(return_value=_images_response(_PNG_A))
    raw = respx.post("http://raw-flux-forbidden:8000/v1/images/generations").mock(
        return_value=_images_response(_PNG_B)
    )

    import app.services.flux_image_provider as provider_mod
    import app.services.studio_render as render_mod

    provider = await render_mod.get_active_flux_provider()
    assert provider is not None and provider.via_csp_runtime is True
    with pytest.raises(provider_mod.FluxBackendError, match="StudioRuntimeContext"):
        await provider.get_or_generate(
            "missing context", use_case=ImageUseCase.COVER_HERO, seed=4
        )

    monkeypatch.setattr(settings, "STUDIO_RUNTIME_SERVICE_TOKEN", "")
    with use_runtime_context(_runtime_context()):
        with pytest.raises(provider_mod.FluxBackendError, match="runtime token"):
            await provider.get_or_generate(
                "missing token", use_case=ImageUseCase.COVER_HERO, seed=5
            )

    assert governed.call_count == 0
    assert raw.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_formal_csp_images_failure_never_falls_back_to_raw_endpoint(
    monkeypatch, tmp_path
):
    _enable_formal_runtime(monkeypatch)
    monkeypatch.setenv("FLUX_BACKEND_URL", "http://raw-flux-forbidden:8000")
    monkeypatch.setenv("FLUX_CACHE_DIR", str(tmp_path / "fc"))
    respx.get(_PRIMARY_URL).mock(
        return_value=httpx.Response(
            200,
            json=_primary_payload("http://raw-flux-forbidden:8000", "formal-image"),
        )
    )
    governed = respx.post(
        f"{_CSP}/v1/studio-runtime/images/generations"
    ).mock(return_value=httpx.Response(502, json={"detail": "upstream failed"}))
    raw = respx.post("http://raw-flux-forbidden:8000/v1/images/generations").mock(
        return_value=_images_response(_PNG_B)
    )

    import app.services.flux_image_provider as provider_mod
    import app.services.studio_render as render_mod

    with use_runtime_context(_runtime_context()):
        provider = await render_mod.get_active_flux_provider()
        assert provider is not None
        with pytest.raises(provider_mod.FluxBackendError, match="CSP governed Images API failed"):
            await provider.get_or_generate(
                "csp failure", use_case=ImageUseCase.COVER_HERO, seed=6
            )

    assert governed.call_count == 1
    assert raw.call_count == 0


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
