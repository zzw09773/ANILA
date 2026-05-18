"""FluxImageProvider — generate images via flux2-dev with cache + concurrency limit."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.flux_image_provider import FluxImageProvider


def test_provider_construction(tmp_path: Path):
    p = FluxImageProvider(
        flux_url="http://flux2-dev:8000",
        cache_dir=tmp_path / "flux-cache",
        max_concurrent=4,
        timeout_seconds=180.0,
    )
    assert p.cache_dir == tmp_path / "flux-cache"


def test_cache_key_is_deterministic():
    p = FluxImageProvider(
        flux_url="http://flux2-dev:8000",
        cache_dir=Path("/tmp"),
        max_concurrent=4,
    )
    k1 = p._cache_key("a tank in the mountains", "16:9")
    k2 = p._cache_key("a tank in the mountains", "16:9")
    assert k1 == k2
    assert len(k1) == 64  # SHA256 hex
    assert all(c in "0123456789abcdef" for c in k1)


def test_cache_key_differs_on_prompt():
    p = FluxImageProvider(
        flux_url="http://flux2-dev:8000",
        cache_dir=Path("/tmp"),
        max_concurrent=4,
    )
    assert p._cache_key("prompt A", "16:9") != p._cache_key("prompt B", "16:9")


def test_cache_key_differs_on_aspect():
    p = FluxImageProvider(
        flux_url="http://flux2-dev:8000",
        cache_dir=Path("/tmp"),
        max_concurrent=4,
    )
    assert p._cache_key("same prompt", "16:9") != p._cache_key("same prompt", "1:1")


def test_cache_path_uses_key_as_filename(tmp_path: Path):
    p = FluxImageProvider(
        flux_url="http://flux2-dev:8000",
        cache_dir=tmp_path,
        max_concurrent=4,
    )
    path = p._cache_path("a prompt", "16:9")
    assert path.parent == tmp_path
    assert path.suffix == ".png"
    assert path.stem == p._cache_key("a prompt", "16:9")


import httpx
import pytest
import respx


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.mark.asyncio
async def test_cache_hit_returns_existing_file(tmp_path):
    p = FluxImageProvider(
        flux_url="http://flux2-dev:8000",
        cache_dir=tmp_path,
        max_concurrent=4,
    )
    # Pre-populate cache for the expected key
    cache_file = p._cache_path("preexisting prompt", "16:9")
    tmp_path.mkdir(exist_ok=True)
    cache_file.write_bytes(_PNG)

    out = await p.get_or_generate("preexisting prompt", "16:9")
    assert out == _PNG


@pytest.mark.asyncio
@respx.mock
async def test_cache_miss_calls_flux_and_writes_file(tmp_path):
    respx.post("http://flux2-dev:8000/generate").mock(
        return_value=httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})
    )

    p = FluxImageProvider(
        flux_url="http://flux2-dev:8000",
        cache_dir=tmp_path,
        max_concurrent=4,
    )
    out = await p.get_or_generate("fresh prompt", "16:9")

    assert out == _PNG
    cache_file = p._cache_path("fresh prompt", "16:9")
    assert cache_file.exists()
    assert cache_file.read_bytes() == _PNG


@pytest.mark.asyncio
@respx.mock
async def test_subsequent_call_hits_cache(tmp_path):
    route = respx.post("http://flux2-dev:8000/generate").mock(
        return_value=httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})
    )

    p = FluxImageProvider(
        flux_url="http://flux2-dev:8000",
        cache_dir=tmp_path,
        max_concurrent=4,
    )
    await p.get_or_generate("repeated prompt", "16:9")
    await p.get_or_generate("repeated prompt", "16:9")

    # FLUX should have been called exactly once (second was cache hit)
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_cache_miss_sends_correct_body(tmp_path):
    route = respx.post("http://flux2-dev:8000/generate").mock(
        return_value=httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})
    )

    p = FluxImageProvider(
        flux_url="http://flux2-dev:8000",
        cache_dir=tmp_path,
        max_concurrent=4,
    )
    await p.get_or_generate("body test", "1:1")

    import json
    body = json.loads(route.calls.last.request.content)
    assert body == {"prompt": "body test", "aspect_ratio": "1:1"}


@pytest.mark.asyncio
@respx.mock
async def test_creates_cache_dir_if_missing(tmp_path):
    target = tmp_path / "does" / "not" / "exist"
    respx.post("http://flux2-dev:8000/generate").mock(
        return_value=httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})
    )

    p = FluxImageProvider(
        flux_url="http://flux2-dev:8000",
        cache_dir=target,
        max_concurrent=4,
    )
    await p.get_or_generate("any", "16:9")

    assert target.is_dir()
