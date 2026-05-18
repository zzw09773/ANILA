"""End-to-end: SlidesSpec with image_prompt → hydrated spec ready for renderer."""
from __future__ import annotations

import importlib
from pathlib import Path

import httpx
import pytest
import respx


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.mark.asyncio
@respx.mock
async def test_full_pipeline_with_image_prompt(monkeypatch, tmp_path):
    monkeypatch.setenv("FLUX_BACKEND_URL", "http://flux2-dev:8000")
    monkeypatch.setenv("FLUX_CACHE_DIR", str(tmp_path / "flux-cache"))
    monkeypatch.setenv("FLUX_MAX_CONCURRENT", "2")
    monkeypatch.setenv("INGESTION_UPLOAD_DIR", str(tmp_path / "uploads"))

    import app.api.studio as studio
    importlib.reload(studio)

    respx.post("http://flux2-dev:8000/generate").mock(
        return_value=httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})
    )

    spec = {"slides": [
        {"title": "Mountain Patrol", "bullets": ["a", "b"],
         "image_prompt": "Soldiers patrolling a misty mountain at dawn, cinematic"},
    ]}

    result = await studio._hydrate_images(
        spec, {}, str(tmp_path / "uploads"),
        flux_provider=studio.get_flux_provider(),
        default_aspect="16:9",
    )

    s = result["slides"][0]
    assert s["image_data"].startswith("data:image/png;base64,")
    # The cache file should also exist
    cache = (tmp_path / "flux-cache").iterdir()
    cache_files = list(cache)
    assert len(cache_files) == 1
    assert cache_files[0].suffix == ".png"


@pytest.mark.asyncio
@respx.mock
async def test_three_slides_share_one_cache_entry(monkeypatch, tmp_path):
    """Three slides with the SAME image_prompt should only call FLUX once."""
    monkeypatch.setenv("FLUX_BACKEND_URL", "http://flux2-dev:8000")
    monkeypatch.setenv("FLUX_CACHE_DIR", str(tmp_path / "fc"))
    monkeypatch.setenv("INGESTION_UPLOAD_DIR", str(tmp_path / "u"))

    import app.api.studio as studio
    importlib.reload(studio)

    route = respx.post("http://flux2-dev:8000/generate").mock(
        return_value=httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})
    )

    same_prompt = "A repeating banner illustration for section dividers"
    spec = {"slides": [
        {"title": f"Section {i}", "bullets": ["a"], "image_prompt": same_prompt}
        for i in range(3)
    ]}

    result = await studio._hydrate_images(
        spec, {}, str(tmp_path / "u"),
        flux_provider=studio.get_flux_provider(),
        default_aspect="16:9",
    )

    for s in result["slides"]:
        assert s["image_data"].startswith("data:image/png;base64,")
    assert route.call_count == 1  # cache made 2 of 3 hit


@pytest.mark.asyncio
@respx.mock
async def test_flux_failure_falls_back_silently(monkeypatch, tmp_path):
    monkeypatch.setenv("FLUX_BACKEND_URL", "http://flux2-dev:8000")
    monkeypatch.setenv("FLUX_CACHE_DIR", str(tmp_path / "fc"))
    monkeypatch.setenv("INGESTION_UPLOAD_DIR", str(tmp_path / "u"))

    import app.api.studio as studio
    importlib.reload(studio)

    respx.post("http://flux2-dev:8000/generate").mock(
        return_value=httpx.Response(503, json={"detail": "model loading"})
    )

    spec = {"slides": [
        {"title": "X", "bullets": ["a"], "image_prompt": "will fail"},
    ]}

    result = await studio._hydrate_images(
        spec, {}, str(tmp_path / "u"),
        flux_provider=studio.get_flux_provider(),
        default_aspect="16:9",
    )

    s = result["slides"][0]
    assert "image_data" not in s
    assert "image_prompt" not in s  # popped on failure
