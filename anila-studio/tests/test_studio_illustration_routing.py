"""Stage 4 — illustration routing (use_case inference / fallback / helper / routing)."""
from __future__ import annotations

import pytest

from app.api.studio import _infer_image_use_case
from app.schemas.studio import ImageUseCase


def test_use_case_idx0_is_hero():
    assert _infer_image_use_case(0, {"layout_kind": "standard"}) is ImageUseCase.COVER_HERO


def test_use_case_idx0_section_break_still_hero():
    # cover is commonly tagged layout_kind=section_break; idx 0 wins → HERO.
    assert _infer_image_use_case(0, {"layout_kind": "section_break"}) is ImageUseCase.COVER_HERO


def test_use_case_layout_cover_is_hero():
    assert _infer_image_use_case(3, {"layout_kind": "cover"}) is ImageUseCase.COVER_HERO


def test_use_case_section_break_is_band():
    assert _infer_image_use_case(2, {"layout_kind": "section_break"}) is ImageUseCase.SECTION_BAND


def test_use_case_default_is_content():
    assert _infer_image_use_case(4, {"layout_kind": "standard"}) is ImageUseCase.CONTENT_ILLUSTRATION


from app.api.studio import _apply_illustration_fallback


def test_fallback_hero_label_and_drops_image():
    slide = {"image_data": "x", "image_prompt": "p", "image_kind": "illustration"}
    _apply_illustration_fallback(slide, ImageUseCase.COVER_HERO)
    assert slide["image_gen_meta"]["fallback"] == "solid_theme_cover"
    assert slide["image_gen_meta"]["use_case"] == "cover_hero"
    assert "image_data" not in slide
    assert "image_prompt" not in slide
    assert "image_kind" not in slide


def test_fallback_band_label():
    slide = {}
    _apply_illustration_fallback(slide, ImageUseCase.SECTION_BAND)
    assert slide["image_gen_meta"]["fallback"] == "theme_section_break"


def test_fallback_content_label():
    slide = {"diagram_dot": "d"}
    _apply_illustration_fallback(slide, ImageUseCase.CONTENT_ILLUSTRATION)
    assert slide["image_gen_meta"]["fallback"] == "text_only"
    assert "diagram_dot" not in slide


import app.api.studio as studio_mod
from app.services.flux_image_provider import GeneratedImage


class _StubVlm:
    def __init__(self, *a, **k):  # accepts _Gemma4VlmGate(db, user) shape
        pass


class _StubLLM:
    _bearer = "stub-bearer"


def _patch_vlm(monkeypatch):
    monkeypatch.setattr(studio_mod, "_Gemma4VlmGate", _StubVlm)


def _patch_rewriter(monkeypatch, returns):
    async def _fake(*, title, bullets, use_case, style, llm):
        _fake.seen = {"title": title, "bullets": bullets, "use_case": use_case}
        return returns
    monkeypatch.setattr(
        "app.services.flux_prompt_rewriter.derive_flux_prompt", _fake
    )
    return _fake


@pytest.mark.asyncio
async def test_helper_success_sets_image_and_meta(monkeypatch):
    _patch_vlm(monkeypatch)
    _patch_rewriter(monkeypatch, "a calm teal abstract scene")
    accepted = GeneratedImage(png_bytes=b"\x89PNGfake", seed=1007, accepted=True)
    accepted.vlm_verdict = {"match": True, "has_text": False, "score": 0.9}

    async def _fake_gate(provider, prompt, *, use_case, seed, style_id, concept_en, vlm):
        return accepted, 0
    monkeypatch.setattr(studio_mod, "_gated_generate", _fake_gate)

    slide = {"title": "Resilience", "bullets": ["a", "b"], "image_prompt": "IGNORED"}
    ok = await studio_mod._generate_slide_illustration(
        slide, idx=7, use_case=ImageUseCase.CONTENT_ILLUSTRATION,
        deck_style=None, flux_provider=object(), deck_base_seed=1000, llm=_StubLLM(),
    )
    assert ok is True
    assert slide["image_data"].startswith("data:image/png;base64,")
    assert slide["image_gen_meta"]["use_case"] == "content_illustration"
    assert slide["image_gen_meta"]["vlm_verdict"]["score"] == 0.9


@pytest.mark.asyncio
async def test_helper_uses_title_bullets_not_image_prompt(monkeypatch):
    _patch_vlm(monkeypatch)
    fake = _patch_rewriter(monkeypatch, "scene")
    accepted = GeneratedImage(png_bytes=b"x", seed=1, accepted=True)
    accepted.vlm_verdict = {"match": True, "has_text": False, "score": 1.0}

    async def _fake_gate(provider, prompt, *, use_case, seed, style_id, concept_en, vlm):
        return accepted, 0
    monkeypatch.setattr(studio_mod, "_gated_generate", _fake_gate)

    slide = {"title": "T", "bullets": ["x"], "image_prompt": "DO NOT USE"}
    await studio_mod._generate_slide_illustration(
        slide, idx=1, use_case=ImageUseCase.CONTENT_ILLUSTRATION,
        deck_style=None, flux_provider=object(), deck_base_seed=1000, llm=_StubLLM(),
    )
    assert fake.seen["title"] == "T"
    assert fake.seen["bullets"] == ["x"]


@pytest.mark.asyncio
async def test_helper_rewriter_none_returns_false(monkeypatch):
    _patch_vlm(monkeypatch)
    _patch_rewriter(monkeypatch, None)  # USE_GRAPHVIZ / stripped empty
    slide = {"title": "T", "bullets": ["x"]}
    ok = await studio_mod._generate_slide_illustration(
        slide, idx=1, use_case=ImageUseCase.CONTENT_ILLUSTRATION,
        deck_style=None, flux_provider=object(), deck_base_seed=1000, llm=_StubLLM(),
    )
    assert ok is False
    assert "image_data" not in slide


@pytest.mark.asyncio
async def test_helper_gate_reject_returns_false(monkeypatch):
    _patch_vlm(monkeypatch)
    _patch_rewriter(monkeypatch, "scene")

    async def _fake_gate(provider, prompt, *, use_case, seed, style_id, concept_en, vlm):
        return None, 3
    monkeypatch.setattr(studio_mod, "_gated_generate", _fake_gate)
    slide = {"title": "T", "bullets": ["x"]}
    ok = await studio_mod._generate_slide_illustration(
        slide, idx=1, use_case=ImageUseCase.CONTENT_ILLUSTRATION,
        deck_style=None, flux_provider=object(), deck_base_seed=1000, llm=_StubLLM(),
    )
    assert ok is False
    assert "image_data" not in slide


@pytest.mark.asyncio
async def test_routing_triggers_only_illustration_slides(monkeypatch):
    calls = []

    async def _spy(slide, *, idx, use_case, **kw):
        calls.append((idx, use_case))
        return True
    monkeypatch.setattr(studio_mod, "_generate_slide_illustration", _spy)

    slides = [
        {"title": "Cover", "bullets": ["x"], "layout_kind": "section_break"},     # idx0 → HERO
        {"title": "Plain", "bullets": ["x"], "layout_kind": "standard"},          # no marker → skip
        {"title": "Sec", "bullets": ["x"], "layout_kind": "section_break"},       # idx2 → BAND
        {"title": "Ill", "bullets": ["x"], "layout_kind": "standard",
         "image_prompt": "p"},                                                    # idx3 → CONTENT
    ]
    out = await studio_mod._hydrate_images(
        {"slides": slides}, {}, bearer="test-bearer",
        flux_provider=object(), deck_base_seed=1000, llm=object(),
    )
    assert [c[0] for c in calls] == [0, 2, 3]  # plain slide skipped
    assert calls[0][1] is ImageUseCase.COVER_HERO
    assert calls[1][1] is ImageUseCase.SECTION_BAND
    assert calls[2][1] is ImageUseCase.CONTENT_ILLUSTRATION


@pytest.mark.asyncio
async def test_routing_per_deck_cap(monkeypatch):
    from app.api.studio import MAX_GENERATED_IMAGES_PER_DECK

    calls = []

    async def _spy(slide, *, idx, use_case, **kw):
        calls.append(idx)
        return True
    monkeypatch.setattr(studio_mod, "_generate_slide_illustration", _spy)

    n = MAX_GENERATED_IMAGES_PER_DECK + 5
    slides = [
        {"title": f"S{i}", "bullets": ["x"], "layout_kind": "standard",
         "image_prompt": "p"}
        for i in range(n)
    ]
    out = await studio_mod._hydrate_images(
        {"slides": slides}, {}, bearer="test-bearer",
        flux_provider=object(), deck_base_seed=1000, llm=object(),
    )
    assert len(calls) == MAX_GENERATED_IMAGES_PER_DECK
    capped = out["slides"][MAX_GENERATED_IMAGES_PER_DECK]
    assert capped["image_gen_meta"]["fallback"] == "text_only"


@pytest.mark.asyncio
async def test_routing_content_sparse_becomes_image_focus(monkeypatch):
    async def _spy(slide, *, idx, use_case, **kw):
        return True
    monkeypatch.setattr(studio_mod, "_generate_slide_illustration", _spy)

    slides = [
        {"title": "Cover", "bullets": ["x"], "layout_kind": "section_break"},      # idx0 HERO
        {"title": "C", "bullets": ["a", "b"], "layout_kind": "standard",
         "image_prompt": "p"},                                                      # idx1 CONTENT, sparse
    ]
    out = await studio_mod._hydrate_images(
        {"slides": slides}, {}, bearer="test-bearer",
        flux_provider=object(), deck_base_seed=1000, llm=object(),
    )
    # CONTENT success → converted so renderImageFocus will show the image.
    assert out["slides"][1]["layout_kind"] == "image_focus"
    # HERO must NOT be converted — it renders full-bleed via renderSectionBreak.
    assert out["slides"][0]["layout_kind"] == "section_break"


@pytest.mark.asyncio
async def test_routing_content_dense_skips_generation(monkeypatch):
    calls = []

    async def _spy(slide, *, idx, use_case, **kw):
        calls.append(idx)
        return True
    monkeypatch.setattr(studio_mod, "_generate_slide_illustration", _spy)

    slides = [
        {"title": "Cover", "bullets": ["x"], "layout_kind": "section_break"},      # idx0 HERO
        {"title": "Dense", "bullets": ["a", "b", "c", "d"], "layout_kind": "standard",
         "image_prompt": "p"},                                                      # idx1 CONTENT, dense
    ]
    out = await studio_mod._hydrate_images(
        {"slides": slides}, {}, bearer="test-bearer",
        flux_provider=object(), deck_base_seed=1000, llm=object(),
    )
    assert calls == [0]  # dense CONTENT skipped — helper not called for idx1
    assert out["slides"][1]["image_gen_meta"]["fallback"] == "text_only"
    assert out["slides"][1]["layout_kind"] == "standard"  # unchanged


@pytest.mark.asyncio
async def test_routing_band_not_converted_to_image_focus(monkeypatch):
    async def _spy(slide, *, idx, use_case, **kw):
        return True
    monkeypatch.setattr(studio_mod, "_generate_slide_illustration", _spy)

    slides = [
        {"title": "Cover", "bullets": ["x"], "layout_kind": "section_break"},      # idx0 HERO
        {"title": "Sec", "bullets": ["x"], "layout_kind": "section_break"},        # idx1 BAND
    ]
    out = await studio_mod._hydrate_images(
        {"slides": slides}, {}, bearer="test-bearer",
        flux_provider=object(), deck_base_seed=1000, llm=object(),
    )
    assert out["slides"][1]["layout_kind"] == "section_break"  # BAND stays full-bleed
