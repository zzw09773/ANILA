"""End-to-end: SlidesSpec illustration slide → Stage 4 rewriter+gate → hydrated spec.

Stage 4 replaced the legacy "image_prompt → direct FLUX (N=1, no gate)" path:
every illustration slide now goes through the Layer A rewriter (driven by
title+bullets, *ignoring* the LLM-supplied image_prompt) + the quality gate,
which requires the full toolchain (an llm adapter + a deck_base_seed). Seeds are
per-slide (deck_base_seed + idx), so the content-addressable cache is shared
across *job re-runs* (same deck_base_seed), not across slides within one deck.

These e2e tests stub the rewriter and the VLM gate, and mock the FLUX /generate
HTTP call, to exercise the full hydration path deterministically.
"""
from __future__ import annotations

import base64
import importlib

import httpx
import pytest
import respx


# Deliberately NOT a decodable PNG — the striping detector decodes to gray and
# fails open (None → not striping) on undecodable bytes, which is what we want
# so the stubbed-pass VLM is the only gate that decides acceptance.
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_PNG_B64 = base64.b64encode(_PNG).decode("ascii")


def _flux_json() -> httpx.Response:
    """Contract-3.2 JSON response (one PNG candidate)."""
    return httpx.Response(
        200, json={"images": [_PNG_B64], "seed": 0, "meta": {"steps": 28}}
    )


class _StubLLM:
    """Minimal stand-in for _StudioLLMAdapter. The rewriter only needs to be
    passed through; the VLM gate constructor reads ``_db`` / ``_user``."""

    _db = None
    _user = None


class _PassVlm:
    """VLM gate stub that always accepts (match, no text, top score)."""

    def __init__(self, *args, **kwargs) -> None:  # noqa: D401, ANN002, ANN003
        pass

    async def check(self, png_bytes: bytes, *, concept: str) -> dict:  # noqa: ARG002
        return {"match": True, "has_text": False, "score": 1.0, "reason": "ok"}


async def _fake_rewriter(*, title, bullets, use_case, style, llm):  # noqa: ANN001, ARG001
    """Deterministic rewriter stub — same title → same prompt → same cache key."""
    return f"clean abstract editorial illustration of {title}"


def _wire_stage4(monkeypatch, studio) -> None:
    """Stub the two LLM-backed dependencies the gate/rewriter need."""
    monkeypatch.setattr(
        "app.services.flux_prompt_rewriter.derive_flux_prompt", _fake_rewriter
    )
    monkeypatch.setattr(studio, "_Gemma4VlmGate", _PassVlm)


@pytest.mark.asyncio
@respx.mock
async def test_stage4_cover_and_content_illustration(monkeypatch, tmp_path):
    """A cover (idx 0 → HERO) and a content slide (idx 1 → CONTENT) both get a
    gated FLUX image; the content slide is converted to an image_focus layout so
    the renderer will actually display it."""
    monkeypatch.setenv("FLUX_BACKEND_URL", "http://flux2-dev:8000")
    monkeypatch.setenv("FLUX_CACHE_DIR", str(tmp_path / "flux-cache"))
    monkeypatch.setenv("FLUX_MAX_CONCURRENT", "2")
    monkeypatch.setenv("INGESTION_UPLOAD_DIR", str(tmp_path / "uploads"))

    import app.api.studio as studio
    importlib.reload(studio)
    _wire_stage4(monkeypatch, studio)

    respx.post("http://flux2-dev:8000/generate").mock(return_value=_flux_json())

    spec = {"slides": [
        {"title": "Cover", "bullets": ["overview"], "layout_kind": "section_break"},
        {"title": "Resilience", "bullets": ["a", "b"],
         "image_prompt": "ignored under Stage 4", "layout_kind": "standard"},
    ]}

    result = await studio._hydrate_images(
        spec, {}, str(tmp_path / "uploads"),
        flux_provider=studio.get_flux_provider(),
        default_aspect="16:9",
        deck_base_seed=1000,
        llm=_StubLLM(),
    )

    cover, content = result["slides"]
    assert cover["image_data"].startswith("data:image/png;base64,")
    assert cover["image_gen_meta"]["use_case"] == "cover_hero"
    # cover keeps its section_break layout (renders full-bleed); not image_focus
    assert cover["layout_kind"] == "section_break"

    assert content["image_data"].startswith("data:image/png;base64,")
    assert content["image_gen_meta"]["use_case"] == "content_illustration"
    # CONTENT success → converted to image_focus so the renderer shows the image
    assert content["layout_kind"] == "image_focus"

    # Two distinct images cached (different use_case aspect + per-slide seed).
    cache_pngs = list((tmp_path / "flux-cache").glob("*.png"))
    assert len(cache_pngs) == 2


@pytest.mark.asyncio
@respx.mock
async def test_stage4_rerun_hits_cache(monkeypatch, tmp_path):
    """Re-running the same job (same deck_base_seed) hits the content-addressable
    cache: FLUX is called on the first run only."""
    monkeypatch.setenv("FLUX_BACKEND_URL", "http://flux2-dev:8000")
    monkeypatch.setenv("FLUX_CACHE_DIR", str(tmp_path / "fc"))
    monkeypatch.setenv("INGESTION_UPLOAD_DIR", str(tmp_path / "u"))

    import app.api.studio as studio
    importlib.reload(studio)
    _wire_stage4(monkeypatch, studio)

    route = respx.post("http://flux2-dev:8000/generate").mock(
        return_value=_flux_json()
    )
    provider = studio.get_flux_provider()

    def _spec() -> dict:
        # Fresh dict each run (hydration mutates slides in place).
        return {"slides": [
            {"title": "Banner", "bullets": ["a"],
             "image_prompt": "x", "layout_kind": "standard"},
        ]}

    first = await studio._hydrate_images(
        _spec(), {}, str(tmp_path / "u"),
        flux_provider=provider, default_aspect="16:9",
        deck_base_seed=2000, llm=_StubLLM(),
    )
    assert first["slides"][0]["image_data"].startswith("data:image/png;base64,")
    calls_after_first = route.call_count
    assert calls_after_first >= 1

    second = await studio._hydrate_images(
        _spec(), {}, str(tmp_path / "u"),
        flux_provider=provider, default_aspect="16:9",
        deck_base_seed=2000, llm=_StubLLM(),
    )
    assert second["slides"][0]["image_data"].startswith("data:image/png;base64,")
    # No new FLUX calls on the re-run — served from cache.
    assert route.call_count == calls_after_first


@pytest.mark.asyncio
@respx.mock
async def test_stage4_flux_failure_falls_back_silently(monkeypatch, tmp_path):
    """When FLUX is down, the gate exhausts retries and the slide falls back to a
    no-image layout (image fields dropped); the deck still renders."""
    monkeypatch.setenv("FLUX_BACKEND_URL", "http://flux2-dev:8000")
    monkeypatch.setenv("FLUX_CACHE_DIR", str(tmp_path / "fc"))
    monkeypatch.setenv("INGESTION_UPLOAD_DIR", str(tmp_path / "u"))

    import app.api.studio as studio
    importlib.reload(studio)
    _wire_stage4(monkeypatch, studio)

    respx.post("http://flux2-dev:8000/generate").mock(
        return_value=httpx.Response(503, json={"detail": "model loading"})
    )

    spec = {"slides": [
        {"title": "X", "bullets": ["a"], "image_prompt": "will fail",
         "layout_kind": "standard"},
    ]}

    result = await studio._hydrate_images(
        spec, {}, str(tmp_path / "u"),
        flux_provider=studio.get_flux_provider(),
        default_aspect="16:9",
        deck_base_seed=3000,
        llm=_StubLLM(),
    )

    s = result["slides"][0]
    assert "image_data" not in s
    assert "image_prompt" not in s  # popped on fallback
