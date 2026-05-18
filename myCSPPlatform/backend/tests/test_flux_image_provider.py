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
