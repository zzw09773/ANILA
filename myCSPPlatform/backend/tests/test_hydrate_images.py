"""_hydrate_images: resolve image_ref AND image_prompt into image_data."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.api.studio import _hydrate_images
from app.services.flux_image_provider import FluxBackendError


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture
def upload_dir(tmp_path: Path) -> Path:
    d = tmp_path / "uploads"
    d.mkdir()
    return d


@pytest.fixture
def existing_image(upload_dir: Path) -> dict:
    """One pre-existing ingestion image on disk."""
    p = upload_dir / "img-abc.png"
    p.write_bytes(_PNG)
    return {"img-abc": {"storage_path": "img-abc.png", "mime": "image/png"}}


@pytest.mark.asyncio
async def test_hydrate_image_ref_unchanged_behavior(upload_dir, existing_image):
    """image_ref path still works — Task 5 must not regress."""
    flux = AsyncMock()
    flux.get_or_generate.return_value = _PNG  # unused on this slide

    spec = {"slides": [{"title": "X", "bullets": ["a"], "image_ref": "img-abc"}]}
    result = await _hydrate_images(
        spec, existing_image, str(upload_dir), flux_provider=flux, default_aspect="16:9"
    )

    s = result["slides"][0]
    assert "image_data" in s
    assert s["image_data"].startswith("data:image/png;base64,")
    flux.get_or_generate.assert_not_called()


@pytest.mark.asyncio
async def test_hydrate_image_prompt_calls_flux(upload_dir, existing_image):
    flux = AsyncMock()
    flux.get_or_generate.return_value = _PNG

    spec = {"slides": [{"title": "Y", "bullets": ["b"], "image_prompt": "a tank"}]}
    result = await _hydrate_images(
        spec, existing_image, str(upload_dir), flux_provider=flux, default_aspect="16:9"
    )

    s = result["slides"][0]
    assert "image_data" in s
    assert s["image_data"].startswith("data:image/png;base64,")
    flux.get_or_generate.assert_awaited_once_with("a tank", "16:9")


@pytest.mark.asyncio
async def test_image_ref_wins_over_image_prompt(upload_dir, existing_image):
    """If both set, prefer image_ref (existing curated content)."""
    flux = AsyncMock()

    spec = {"slides": [{
        "title": "Z", "bullets": ["c"],
        "image_ref": "img-abc",
        "image_prompt": "should not be called",
    }]}
    result = await _hydrate_images(
        spec, existing_image, str(upload_dir), flux_provider=flux, default_aspect="16:9"
    )

    assert result["slides"][0]["image_data"].startswith("data:image/png;base64,")
    flux.get_or_generate.assert_not_called()


@pytest.mark.asyncio
async def test_flux_failure_drops_image_prompt(upload_dir, existing_image):
    """When FLUX fails, drop image_prompt so renderer falls back to
    standard layout — same fallback as a bad image_ref."""
    flux = AsyncMock()
    flux.get_or_generate.side_effect = FluxBackendError("boom")

    spec = {"slides": [{
        "title": "W", "bullets": ["d"],
        "image_prompt": "this will fail",
    }]}
    result = await _hydrate_images(
        spec, existing_image, str(upload_dir), flux_provider=flux, default_aspect="16:9"
    )

    s = result["slides"][0]
    assert "image_data" not in s
    assert "image_prompt" not in s  # popped


@pytest.mark.asyncio
async def test_no_provider_skips_image_prompt(upload_dir, existing_image):
    """If flux_provider is None (FLUX not configured), prompt path is
    skipped silently — slide falls back to standard layout."""
    spec = {"slides": [{
        "title": "V", "bullets": ["e"],
        "image_prompt": "no provider available",
    }]}
    result = await _hydrate_images(
        spec, existing_image, str(upload_dir), flux_provider=None, default_aspect="16:9"
    )

    s = result["slides"][0]
    assert "image_data" not in s


@pytest.mark.asyncio
async def test_mixed_slides_all_resolved(upload_dir, existing_image):
    """A spec with one image_ref slide, one image_prompt slide, and
    one no-image slide — all three resolved correctly."""
    flux = AsyncMock()
    flux.get_or_generate.return_value = _PNG

    spec = {"slides": [
        {"title": "A", "bullets": ["a"], "image_ref": "img-abc"},
        {"title": "B", "bullets": ["b"], "image_prompt": "new image"},
        {"title": "C", "bullets": ["c"]},
    ]}
    result = await _hydrate_images(
        spec, existing_image, str(upload_dir), flux_provider=flux, default_aspect="16:9"
    )

    assert "image_data" in result["slides"][0]
    assert "image_data" in result["slides"][1]
    assert "image_data" not in result["slides"][2]
    flux.get_or_generate.assert_awaited_once()
