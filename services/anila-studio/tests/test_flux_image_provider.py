"""FluxImageProvider — generate images via the FLUX service with cache +
concurrency limit.

Updated for the FLUX Stage 1 locked contracts (spec sections 3.3-3.4) and
the 2026-07 OpenAI Images API migration:
  * `_cache_key` now folds in seed + style_id + steps + guidance (3.3).
  * `get_or_generate` is keyword-only (use_case / seed / ...) and returns
    `list[GeneratedImage]` (3.4).
  * The HTTP call is now the standard OpenAI Images API:
    ``POST {base}/v1/images/generations`` with
    ``{model, prompt, n, size, response_format:"b64_json"}``, receiving
    ``{created, data:[{b64_json}]}``(遷移細節見
    test_flux_openai_images.py)。
"""
from __future__ import annotations

import base64
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


def _flux_response(*, n: int = 1) -> httpx.Response:
    """Build an OpenAI Images API response with `n` identical PNG candidates."""
    return httpx.Response(
        200,
        json={
            "created": 1_720_000_000,
            "data": [{"b64_json": _PNG_B64}] * n,
        },
    )


def _provider(cache_dir: Path, max_concurrent: int = 4) -> FluxImageProvider:
    return FluxImageProvider(
        flux_url="http://flux2-dev:8000",
        cache_dir=cache_dir,
        max_concurrent=max_concurrent,
    )


def test_provider_construction(tmp_path: Path):
    p = FluxImageProvider(
        flux_url="http://flux2-dev:8000",
        cache_dir=tmp_path / "flux-cache",
        max_concurrent=4,
        timeout_seconds=180.0,
    )
    assert p.cache_dir == tmp_path / "flux-cache"


# ── Cache key (contract 3.3) ─────────────────────────────────────────────


def test_cache_key_is_deterministic():
    p = _provider(Path("/tmp"))
    k1 = p._cache_key("a tank in the mountains", "16:9", 100, "default", None, None, "flux.2-dev")
    k2 = p._cache_key("a tank in the mountains", "16:9", 100, "default", None, None, "flux.2-dev")
    assert k1 == k2
    assert len(k1) == 64  # SHA256 hex
    assert all(c in "0123456789abcdef" for c in k1)


def test_cache_key_differs_on_prompt():
    p = _provider(Path("/tmp"))
    assert p._cache_key("prompt A", "16:9", 1, "default", None, None, "flux.2-dev") != p._cache_key(
        "prompt B", "16:9", 1, "default", None, None, "flux.2-dev"
    )


def test_cache_key_differs_on_aspect():
    p = _provider(Path("/tmp"))
    assert p._cache_key("same", "16:9", 1, "default", None, None, "flux.2-dev") != p._cache_key(
        "same", "1:1", 1, "default", None, None, "flux.2-dev"
    )


def test_cache_key_differs_on_seed():
    p = _provider(Path("/tmp"))
    assert p._cache_key("same", "16:9", 1, "default", None, None, "flux.2-dev") != p._cache_key(
        "same", "16:9", 2, "default", None, None, "flux.2-dev"
    )


def test_cache_key_differs_on_style_id():
    p = _provider(Path("/tmp"))
    assert p._cache_key("same", "16:9", 1, "default", None, None, "flux.2-dev") != p._cache_key(
        "same", "16:9", 1, "brandX", None, None, "flux.2-dev"
    )


def test_cache_key_differs_on_model():
    """新增(2026-07-06):image-primary 熱切換後,同一 prompt/seed/style 換了
    model 不能吃到舊 model 生的圖 —— cache key 必須把 model 折進去。"""
    p = _provider(Path("/tmp"))
    assert p._cache_key("same", "16:9", 1, "default", None, None, "flux.2-dev") != p._cache_key(
        "same", "16:9", 1, "default", None, None, "flux.3-pro"
    )


def test_cache_path_uses_key_as_filename(tmp_path: Path):
    p = _provider(tmp_path)
    key = p._cache_key("a prompt", "16:9", 1, "default", None, None, "flux.2-dev")
    path = p._cache_path(key)
    assert path.parent == tmp_path
    assert path.suffix == ".png"
    assert path.stem == key


# ── get_or_generate (contract 3.4) ───────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_hit_returns_existing_file(tmp_path):
    p = _provider(tmp_path)
    # key 手動組出來要含 model,才會撞到 get_or_generate 內部真正用的 cache
    # 檔名(provider 預設 model=flux.2-dev)。
    key = p._cache_key("preexisting prompt", "16:9", 5, "default", None, None, "flux.2-dev")
    tmp_path.mkdir(exist_ok=True)
    p._cache_path(key).write_bytes(_PNG)

    out = await p.get_or_generate(
        "preexisting prompt", use_case=ImageUseCase.COVER_HERO, seed=5,
    )
    assert isinstance(out, list)
    assert len(out) == 1
    assert isinstance(out[0], GeneratedImage)
    assert out[0].png_bytes == _PNG
    assert out[0].accepted is True


@pytest.mark.asyncio
@respx.mock
async def test_cache_miss_calls_flux_and_writes_file(tmp_path):
    respx.post("http://flux2-dev:8000/v1/images/generations").mock(return_value=_flux_response())

    p = _provider(tmp_path)
    out = await p.get_or_generate(
        "fresh prompt", use_case=ImageUseCase.COVER_HERO, seed=7,
    )

    assert out[0].png_bytes == _PNG
    key = p._cache_key("fresh prompt", "16:9", 7, "default", None, None, "flux.2-dev")
    assert p._cache_path(key).read_bytes() == _PNG


@pytest.mark.asyncio
@respx.mock
async def test_subsequent_call_hits_cache(tmp_path):
    route = respx.post("http://flux2-dev:8000/v1/images/generations").mock(
        return_value=_flux_response()
    )

    p = _provider(tmp_path)
    await p.get_or_generate("repeated", use_case=ImageUseCase.COVER_HERO, seed=1)
    await p.get_or_generate("repeated", use_case=ImageUseCase.COVER_HERO, seed=1)

    assert route.call_count == 1  # second was a cache hit


@pytest.mark.asyncio
@respx.mock
async def test_cache_miss_sends_correct_body(tmp_path):
    route = respx.post("http://flux2-dev:8000/v1/images/generations").mock(
        return_value=_flux_response()
    )

    p = _provider(tmp_path)
    await p.get_or_generate(
        "body test",
        use_case=ImageUseCase.CONTENT_ILLUSTRATION,
        seed=42,
        num_candidates=1,
    )

    body = json.loads(route.calls.last.request.content)
    # CONTENT_ILLUSTRATION maps to 4:3 → size 1216x896; num_candidates → n.
    # seed 等非 OpenAI 標準欄位不上 wire。
    assert body == {
        "model": "flux.2-dev",
        "prompt": "body test",
        "n": 1,
        "size": "1216x896",
        "response_format": "b64_json",
    }


@pytest.mark.asyncio
@respx.mock
async def test_returns_all_candidates(tmp_path):
    respx.post("http://flux2-dev:8000/v1/images/generations").mock(
        return_value=_flux_response(n=3)
    )
    p = _provider(tmp_path)
    out = await p.get_or_generate(
        "multi", use_case=ImageUseCase.COVER_HERO, seed=9, num_candidates=3,
    )
    assert len(out) == 3
    assert all(g.png_bytes == _PNG for g in out)


@pytest.mark.asyncio
@respx.mock
async def test_creates_cache_dir_if_missing(tmp_path):
    target = tmp_path / "does" / "not" / "exist"
    respx.post("http://flux2-dev:8000/v1/images/generations").mock(return_value=_flux_response())

    p = _provider(target)
    await p.get_or_generate("any", use_case=ImageUseCase.COVER_HERO, seed=1)

    assert target.is_dir()


@pytest.mark.asyncio
@respx.mock
async def test_raises_flux_backend_error_on_non_200(tmp_path):
    respx.post("http://flux2-dev:8000/v1/images/generations").mock(
        return_value=httpx.Response(500, json={"detail": "OOM"})
    )
    p = _provider(tmp_path)
    with pytest.raises(FluxBackendError, match="500"):
        await p.get_or_generate("oom", use_case=ImageUseCase.COVER_HERO, seed=1)


@pytest.mark.asyncio
@respx.mock
async def test_raises_on_missing_images_list(tmp_path):
    respx.post("http://flux2-dev:8000/v1/images/generations").mock(
        return_value=httpx.Response(200, json={"created": 1})
    )
    p = _provider(tmp_path)
    with pytest.raises(FluxBackendError, match="data"):
        await p.get_or_generate("noimg", use_case=ImageUseCase.COVER_HERO, seed=1)


@pytest.mark.asyncio
async def test_semaphore_limits_concurrent_calls(tmp_path):
    """N concurrent get_or_generate calls — at most max_concurrent in flight."""
    import asyncio

    in_flight = 0
    max_in_flight = 0

    async def slow_handler(request):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return _flux_response()

    with respx.mock(base_url="http://flux2-dev:8000") as router:
        router.post("/v1/images/generations").mock(side_effect=slow_handler)

        p = _provider(tmp_path, max_concurrent=2)
        # Unique seeds so the cache always misses.
        await asyncio.gather(
            *[
                p.get_or_generate(
                    "p", use_case=ImageUseCase.COVER_HERO, seed=i,
                )
                for i in range(6)
            ]
        )

    assert max_in_flight <= 2, f"max_in_flight={max_in_flight}, expected <= 2"


@pytest.mark.asyncio
@respx.mock
async def test_cache_failure_propagates(tmp_path):
    """A failed first call must not populate the cache; retry succeeds."""
    route = respx.post("http://flux2-dev:8000/v1/images/generations").mock(
        side_effect=[
            httpx.Response(500, json={"detail": "warmup"}),
            _flux_response(),
        ]
    )
    p = _provider(tmp_path)

    with pytest.raises(FluxBackendError):
        await p.get_or_generate("retry", use_case=ImageUseCase.COVER_HERO, seed=3)
    key = p._cache_key("retry", "16:9", 3, "default", None, None, "flux.2-dev")
    assert not p._cache_path(key).exists()

    out = await p.get_or_generate("retry", use_case=ImageUseCase.COVER_HERO, seed=3)
    assert out[0].png_bytes == _PNG
    assert route.call_count == 2
