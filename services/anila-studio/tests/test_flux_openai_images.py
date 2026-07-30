"""FluxImageProvider — OpenAI Images API 遷移合約測試。

院內模型統一部署在雲端算力中心後,FLUX 圖像生成改走標準 OpenAI Images API:

    POST {base}/v1/images/generations
    request : {model, prompt, n, size, response_format:"b64_json"}
              (+ 可選 Authorization: Bearer)
    response: {created, data:[{b64_json}]}

本檔驗證:
  * URL 打到 /v1/images/generations,base 有無 /v1 皆正規化(比照 csp
    memory_service._embed 的做法)。
  * body 只含標準欄位;seed / num_inference_steps / guidance_scale 等
    非標準欄位不上 wire。
  * num_candidates → n;use_case 的 aspect_ratio → size 對映表。
  * 設 api_key 才帶 Bearer。
  * data[].b64_json 解碼後,下游依然拿到 list[GeneratedImage](契約 3.4
    不變)。
"""
from __future__ import annotations

import base64
import importlib
import json
from pathlib import Path

import httpx
import pytest
import respx

from app.schemas.studio import ImageUseCase
from app.services.flux_image_provider import (
    FluxBackendError,
    FluxImageProvider,
    GeneratedImage,
)

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_PNG_B64 = base64.b64encode(_PNG).decode("ascii")


def _openai_response(*, n: int = 1) -> httpx.Response:
    """標準 OpenAI Images API 回應(b64_json)。"""
    return httpx.Response(
        200,
        json={
            "created": 1_720_000_000,
            "data": [{"b64_json": _PNG_B64}] * n,
        },
    )


def _provider(
    cache_dir: Path,
    *,
    flux_url: str = "http://flux-images:8000",
    model: str = "flux.2-dev",
    api_key: str = "",
) -> FluxImageProvider:
    return FluxImageProvider(
        flux_url=flux_url,
        cache_dir=cache_dir,
        max_concurrent=4,
        model=model,
        api_key=api_key,
    )


# ── URL / /v1 正規化 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_bare_host_base_gets_v1_appended(tmp_path):
    route = respx.post("http://flux-images:8000/v1/images/generations").mock(
        return_value=_openai_response()
    )
    p = _provider(tmp_path, flux_url="http://flux-images:8000")
    await p.get_or_generate("x", use_case=ImageUseCase.COVER_HERO, seed=1)
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_base_already_with_v1_not_doubled(tmp_path):
    route = respx.post("http://flux-images:8000/v1/images/generations").mock(
        return_value=_openai_response()
    )
    p = _provider(tmp_path, flux_url="http://flux-images:8000/v1")
    await p.get_or_generate("x", use_case=ImageUseCase.COVER_HERO, seed=1)
    assert route.called
    # 沒有拼成 /v1/v1/...
    assert "/v1/v1/" not in str(route.calls.last.request.url)


# ── Request body:標準欄位、非標準欄位不上 wire ─────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_body_is_standard_openai_images_request(tmp_path):
    route = respx.post("http://flux-images:8000/v1/images/generations").mock(
        return_value=_openai_response()
    )
    p = _provider(tmp_path)
    await p.get_or_generate(
        "body test",
        use_case=ImageUseCase.CONTENT_ILLUSTRATION,
        seed=42,
        num_candidates=1,
        steps=28,
        guidance=4.0,
    )
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "model": "flux.2-dev",
        "prompt": "body test",
        "n": 1,
        "size": "1216x896",  # 4:3 對映
        "response_format": "b64_json",
    }
    # 非 OpenAI 標準欄位一律不上 wire
    for banned in (
        "seed",
        "num_candidates",
        "num_inference_steps",
        "guidance_scale",
        "aspect_ratio",
    ):
        assert banned not in body


@pytest.mark.asyncio
@respx.mock
async def test_num_candidates_maps_to_n(tmp_path):
    route = respx.post("http://flux-images:8000/v1/images/generations").mock(
        return_value=_openai_response(n=3)
    )
    p = _provider(tmp_path)
    out = await p.get_or_generate(
        "multi", use_case=ImageUseCase.COVER_HERO, seed=9, num_candidates=3
    )
    body = json.loads(route.calls.last.request.content)
    assert body["n"] == 3
    assert len(out) == 3


@pytest.mark.asyncio
@respx.mock
async def test_aspect_ratio_to_size_mapping(tmp_path):
    """use_case → aspect → size 對映表(16:9 / 3:1 / 4:3 全走一遍)。"""
    route = respx.post("http://flux-images:8000/v1/images/generations").mock(
        return_value=_openai_response()
    )
    p = _provider(tmp_path)
    expected = {
        ImageUseCase.COVER_HERO: "1792x1024",  # 16:9
        ImageUseCase.SECTION_BAND: "1536x512",  # 3:1
        ImageUseCase.CONTENT_ILLUSTRATION: "1216x896",  # 4:3
    }
    for idx, (use_case, size) in enumerate(expected.items()):
        await p.get_or_generate(f"p{idx}", use_case=use_case, seed=idx)
        body = json.loads(route.calls.last.request.content)
        assert body["size"] == size, use_case


@pytest.mark.asyncio
@respx.mock
async def test_generate_candidates_also_speaks_openai(tmp_path):
    """Stage 2 gate 路徑(generate_candidates)同樣走新合約。"""
    route = respx.post("http://flux-images:8000/v1/images/generations").mock(
        return_value=_openai_response(n=2)
    )
    p = _provider(tmp_path)
    out = await p.generate_candidates(
        "gate", use_case=ImageUseCase.COVER_HERO, seed=5, num_candidates=2
    )
    body = json.loads(route.calls.last.request.content)
    assert body["n"] == 2
    assert body["size"] == "1792x1024"
    assert "seed" not in body
    assert len(out) == 2
    assert all(not g.accepted for g in out)


# ── Bearer ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_bearer_sent_when_api_key_set(tmp_path):
    route = respx.post("http://flux-images:8000/v1/images/generations").mock(
        return_value=_openai_response()
    )
    p = _provider(tmp_path, api_key="sk-test-123")
    await p.get_or_generate("auth", use_case=ImageUseCase.COVER_HERO, seed=1)
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-test-123"


@pytest.mark.asyncio
@respx.mock
async def test_no_bearer_when_api_key_empty(tmp_path):
    route = respx.post("http://flux-images:8000/v1/images/generations").mock(
        return_value=_openai_response()
    )
    p = _provider(tmp_path, api_key="")
    await p.get_or_generate("noauth", use_case=ImageUseCase.COVER_HERO, seed=1)
    assert "authorization" not in route.calls.last.request.headers


# ── Response 解析:下游格式不變 ──────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_b64_json_decoded_to_generated_image(tmp_path):
    respx.post("http://flux-images:8000/v1/images/generations").mock(
        return_value=_openai_response()
    )
    p = _provider(tmp_path)
    out = await p.get_or_generate("dl", use_case=ImageUseCase.COVER_HERO, seed=7)
    assert isinstance(out, list) and len(out) == 1
    assert isinstance(out[0], GeneratedImage)
    assert out[0].png_bytes == _PNG
    assert out[0].seed == 7  # OpenAI 回應無 seed → 回填 caller 的 seed
    assert out[0].accepted is True


@pytest.mark.asyncio
@respx.mock
async def test_cache_still_written_and_hit(tmp_path):
    route = respx.post("http://flux-images:8000/v1/images/generations").mock(
        return_value=_openai_response()
    )
    p = _provider(tmp_path)
    await p.get_or_generate("repeat", use_case=ImageUseCase.COVER_HERO, seed=1)
    await p.get_or_generate("repeat", use_case=ImageUseCase.COVER_HERO, seed=1)
    assert route.call_count == 1  # 第二次 cache hit


@pytest.mark.asyncio
@respx.mock
async def test_raises_on_missing_data_list(tmp_path):
    respx.post("http://flux-images:8000/v1/images/generations").mock(
        return_value=httpx.Response(200, json={"created": 1})
    )
    p = _provider(tmp_path)
    with pytest.raises(FluxBackendError):
        await p.get_or_generate("nodata", use_case=ImageUseCase.COVER_HERO, seed=1)


@pytest.mark.asyncio
@respx.mock
async def test_raises_on_malformed_base64(tmp_path):
    """b64decode 預設靜默丟棄非法字元（"!!!!" → b""）——必須 validate=True
    才會 raise，否則空 bytes 會進 cache 變成壞圖（Codex 審查發現）。"""
    respx.post("http://flux-images:8000/v1/images/generations").mock(
        return_value=httpx.Response(200, json={"created": 1, "data": [{"b64_json": "!!!!"}]})
    )
    p = _provider(tmp_path)
    with pytest.raises(FluxBackendError):
        await p.get_or_generate("badb64", use_case=ImageUseCase.COVER_HERO, seed=1)


@pytest.mark.asyncio
@respx.mock
async def test_raises_on_empty_b64_payload(tmp_path):
    """合法但空的 base64（"" → b""）也必須 fail-loud，不得產出空圖。"""
    respx.post("http://flux-images:8000/v1/images/generations").mock(
        return_value=httpx.Response(200, json={"created": 1, "data": [{"b64_json": ""}]})
    )
    p = _provider(tmp_path)
    with pytest.raises(FluxBackendError, match="empty image payload"):
        await p.get_or_generate("emptyb64", use_case=ImageUseCase.COVER_HERO, seed=1)


@pytest.mark.asyncio
@respx.mock
async def test_raises_on_non_200(tmp_path):
    respx.post("http://flux-images:8000/v1/images/generations").mock(
        return_value=httpx.Response(500, json={"error": {"message": "boom"}})
    )
    p = _provider(tmp_path)
    with pytest.raises(FluxBackendError, match="500"):
        await p.get_or_generate("err", use_case=ImageUseCase.COVER_HERO, seed=1)


# ── env 佈線(FLUX_MODEL / FLUX_API_KEY)────────────────────────────────


def test_get_flux_provider_wires_model_and_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("FLUX_BACKEND_URL", "http://flux-images:8000/v1")
    monkeypatch.setenv("FLUX_CACHE_DIR", str(tmp_path / "fc"))
    monkeypatch.setenv("FLUX_MODEL", "flux-pro")
    monkeypatch.setenv("FLUX_API_KEY", "sk-wired")
    import app.services.studio_render as studio_render

    importlib.reload(studio_render)
    p = studio_render.get_flux_provider()
    assert p is not None
    assert p.model == "flux-pro"
    assert p.api_key == "sk-wired"
    # 還原 singleton,避免污染其他測試
    importlib.reload(studio_render)
