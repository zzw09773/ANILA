"""_hydrate_images: resolve image_ref / diagram_dot / image_prompt into image_data.

Phase 4 (2026-05-23) — adapted from csp baseline for anila-studio:
- `upload_dir` positional argument removed; `bearer` keyword required.
- `image_ref` resolution now flows through ``csp_client.fetch_image_blob``
  (HTTP) rather than reading from a shared upload_dir mount. Tests patch
  ``app.services.studio_render.fetch_image_blob`` (where _hydrate_images now
  lives after the god-module split) to inject deterministic bytes.
- ``images_lookup`` shape: ``{image_id_str: {"image_id": int, "mime": str}}``
  (the studio module casts ``meta["image_id"]`` to ``int`` before calling
  ``fetch_image_blob``).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.api.studio import _hydrate_images
from app.schemas.studio import ImageUseCase
from app.services.flux_image_provider import FluxBackendError, GeneratedImage


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_BEARER = "test-bearer-token"


def _gen(seed: int = 0) -> list[GeneratedImage]:
    """A one-candidate result list matching the contract-3.4 return type."""
    return [GeneratedImage(png_bytes=_PNG, seed=seed, accepted=True)]


@pytest.fixture
def existing_image() -> dict:
    """One pre-existing ingestion image lookup entry keyed by image_id.

    The studio module reads ``meta["image_id"]`` and calls
    ``fetch_image_blob(int(meta["image_id"]), bearer=...)`` — tests mock
    that function instead of writing to disk.
    """
    return {"img-abc": {"image_id": 42, "mime": "image/png"}}


@pytest.fixture
def fetch_blob_mock():
    """Patch ``fetch_image_blob`` to return ``(_PNG, "image/png")``.

    Yields the mock so individual tests can assert on call args if needed.
    """
    with patch(
        "app.services.studio_render.fetch_image_blob",
        new=AsyncMock(return_value=(_PNG, "image/png")),
    ) as m:
        yield m


@pytest.mark.asyncio
async def test_hydrate_image_ref_unchanged_behavior(existing_image, fetch_blob_mock):
    """image_ref path still works — Task 5 must not regress."""
    flux = AsyncMock()
    flux.get_or_generate.return_value = _PNG  # unused on this slide

    spec = {"slides": [{"title": "X", "bullets": ["a"], "image_ref": "img-abc"}]}
    result = await _hydrate_images(
        spec, existing_image, bearer=_BEARER, flux_provider=flux, default_aspect="16:9"
    )

    s = result["slides"][0]
    assert "image_data" in s
    assert s["image_data"].startswith("data:image/png;base64,")
    flux.get_or_generate.assert_not_called()
    fetch_blob_mock.assert_awaited_once_with(42, bearer=_BEARER)


@pytest.mark.asyncio
async def test_hydrate_image_prompt_calls_flux(existing_image, fetch_blob_mock):
    flux = AsyncMock()
    flux.get_or_generate.return_value = _gen()

    # Slide index 1 (not the cover) so the legacy image_prompt path runs.
    spec = {"slides": [
        {"title": "cover", "bullets": ["x"]},
        {"title": "Y", "bullets": ["b"], "image_prompt": "a tank"},
    ]}
    result = await _hydrate_images(
        spec, existing_image, bearer=_BEARER, flux_provider=flux, default_aspect="16:9"
    )

    s = result["slides"][1]
    assert "image_data" in s
    assert s["image_data"].startswith("data:image/png;base64,")
    # Legacy path now uses the contract-3.4 keyword signature; no deck seed
    # passed → legacy_seed defaults to 0, CONTENT_ILLUSTRATION aspect.
    flux.get_or_generate.assert_awaited_once_with(
        "a tank",
        use_case=ImageUseCase.CONTENT_ILLUSTRATION,
        seed=0,
        num_candidates=1,
    )


@pytest.mark.asyncio
async def test_cover_hero_path_generates_via_rewriter(fetch_blob_mock):
    """FLUX Stage 1 cover hero: slide index 0 with deck_base_seed + llm →
    rewriter produces a prompt, provider generates, image_gen_meta filled."""
    flux = AsyncMock()
    flux.get_or_generate.return_value = _gen(seed=12345)

    llm = AsyncMock()

    spec = {"slides": [{"title": "韌性網路", "bullets": ["a", "b"]}]}

    with patch(
        "app.services.flux_prompt_rewriter.derive_flux_prompt",
        new=AsyncMock(return_value="a resilient lattice of light, soft glow, flat style"),
    ) as mock_rw:
        result = await _hydrate_images(
            spec, {}, bearer=_BEARER,
            flux_provider=flux, default_aspect="16:9",
            deck_base_seed=1000, llm=llm,
        )

    s = result["slides"][0]
    assert s["image_data"].startswith("data:image/png;base64,")
    assert s["image_gen_meta"]["use_case"] == "cover_hero"
    assert s["image_gen_meta"]["seed"] == 12345
    assert s["image_gen_meta"]["style_id"] == "default"
    assert "flux_prompt" in s["image_gen_meta"]
    mock_rw.assert_awaited_once()
    # Provider called with COVER_HERO + deterministic seed (base + index 0).
    _, kwargs = flux.get_or_generate.call_args
    assert kwargs["use_case"] == ImageUseCase.COVER_HERO
    assert kwargs["seed"] == 1000


@pytest.mark.asyncio
async def test_cover_hero_skipped_without_seed_or_llm(fetch_blob_mock):
    """No deck_base_seed / llm → cover-hero path is inert; legacy behaviour."""
    flux = AsyncMock()
    flux.get_or_generate.return_value = _gen()

    spec = {"slides": [{"title": "cover", "bullets": ["a"]}]}
    result = await _hydrate_images(
        spec, {}, bearer=_BEARER, flux_provider=flux, default_aspect="16:9",
    )
    # No image_prompt and no cover-hero wiring → nothing generated.
    assert "image_data" not in result["slides"][0]
    flux.get_or_generate.assert_not_called()


@pytest.mark.asyncio
async def test_image_ref_wins_over_image_prompt(existing_image, fetch_blob_mock):
    """If both set, prefer image_ref (existing curated content)."""
    flux = AsyncMock()

    spec = {"slides": [{
        "title": "Z", "bullets": ["c"],
        "image_ref": "img-abc",
        "image_prompt": "should not be called",
    }]}
    result = await _hydrate_images(
        spec, existing_image, bearer=_BEARER, flux_provider=flux, default_aspect="16:9"
    )

    assert result["slides"][0]["image_data"].startswith("data:image/png;base64,")
    flux.get_or_generate.assert_not_called()


@pytest.mark.asyncio
async def test_flux_failure_drops_image_prompt(existing_image, fetch_blob_mock):
    """When FLUX fails, drop image_prompt so renderer falls back to
    standard layout — same fallback as a bad image_ref."""
    flux = AsyncMock()
    flux.get_or_generate.side_effect = FluxBackendError("boom")

    spec = {"slides": [{
        "title": "W", "bullets": ["d"],
        "image_prompt": "this will fail",
    }]}
    result = await _hydrate_images(
        spec, existing_image, bearer=_BEARER, flux_provider=flux, default_aspect="16:9"
    )

    s = result["slides"][0]
    assert "image_data" not in s
    assert "image_prompt" not in s  # popped


@pytest.mark.asyncio
async def test_no_provider_skips_image_prompt(existing_image, fetch_blob_mock):
    """If flux_provider is None (FLUX not configured), prompt path is
    skipped silently — slide falls back to standard layout."""
    spec = {"slides": [{
        "title": "V", "bullets": ["e"],
        "image_prompt": "no provider available",
    }]}
    result = await _hydrate_images(
        spec, existing_image, bearer=_BEARER, flux_provider=None, default_aspect="16:9"
    )

    s = result["slides"][0]
    assert "image_data" not in s


@pytest.mark.asyncio
async def test_diagram_dot_renders_via_graphviz(existing_image, fetch_blob_mock):
    """Studio Fix 2: image_kind='diagram' + diagram_dot → inline PNG.

    FLUX must NOT be called for diagram slides — diagrams need crisp
    text labels that FLUX can't produce.
    """
    flux = AsyncMock()  # should remain uncalled

    spec = {"slides": [{
        "title": "Architecture",
        "bullets": ["overview"],
        "image_kind": "diagram",
        "diagram_dot": "digraph G { A -> B }",
    }]}

    with patch(
        "app.services.diagram_renderer.asyncio.create_subprocess_exec"
    ) as mock_exec:
        fake_proc = AsyncMock()
        fake_proc.returncode = 0
        fake_proc.communicate = AsyncMock(
            return_value=(_PNG, b"")
        )
        mock_exec.return_value = fake_proc

        result = await _hydrate_images(
            spec, existing_image, bearer=_BEARER,
            flux_provider=flux, default_aspect="16:9",
        )

    s = result["slides"][0]
    assert "image_data" in s
    assert s["image_data"].startswith("data:image/png;base64,")
    # Diagram fields consumed (renderer doesn't see them, image_data won)
    assert "diagram_dot" not in s
    assert "image_kind" not in s
    flux.get_or_generate.assert_not_called()


@pytest.mark.asyncio
async def test_diagram_render_failure_drops_dot(existing_image, fetch_blob_mock):
    """When `dot` returns None (syntax error / missing binary / timeout),
    drop diagram_dot + image_kind so renderer falls back to standard.

    image_prompt is intentionally NOT tried as a fallback: the LLM
    declared this a diagram, not an illustration; sending it to FLUX
    would put garbled-text output back on the slide.
    """
    flux = AsyncMock()  # should remain uncalled

    spec = {"slides": [{
        "title": "Broken diagram",
        "bullets": ["x"],
        "image_kind": "diagram",
        "diagram_dot": "digraph { bad syntax",
    }]}

    with patch(
        "app.services.diagram_renderer.asyncio.create_subprocess_exec",
        side_effect=FileNotFoundError("dot not installed"),
    ):
        result = await _hydrate_images(
            spec, existing_image, bearer=_BEARER,
            flux_provider=flux, default_aspect="16:9",
        )

    s = result["slides"][0]
    assert "image_data" not in s
    assert "diagram_dot" not in s
    assert "image_kind" not in s
    flux.get_or_generate.assert_not_called()


@pytest.mark.asyncio
async def test_mixed_slides_all_resolved(existing_image, fetch_blob_mock):
    """A spec with one image_ref slide, one image_prompt slide, and
    one no-image slide — all three resolved correctly."""
    flux = AsyncMock()
    flux.get_or_generate.return_value = _gen()

    spec = {"slides": [
        {"title": "A", "bullets": ["a"], "image_ref": "img-abc"},
        {"title": "B", "bullets": ["b"], "image_prompt": "new image"},
        {"title": "C", "bullets": ["c"]},
    ]}
    result = await _hydrate_images(
        spec, existing_image, bearer=_BEARER, flux_provider=flux, default_aspect="16:9"
    )

    assert "image_data" in result["slides"][0]
    assert "image_data" in result["slides"][1]
    assert "image_data" not in result["slides"][2]
    flux.get_or_generate.assert_awaited_once()
