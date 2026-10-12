"""Stage 4 — illustration routing (use_case inference / fallback / helper / routing)."""
from __future__ import annotations

import pytest

from app.api.studio import _infer_image_use_case
from app.schemas.studio import ImageUseCase


@pytest.mark.parametrize(
    "infer_image_use_case_arg, item",
    [
        pytest.param(0, "standard", id='idx0_is_hero'),
        pytest.param(0, "section_break", id='idx0_section_break_still_hero'),
        pytest.param(3, "cover", id='layout_cover_is_hero'),
    ],
)
def test_use_case(infer_image_use_case_arg, item):
    assert _infer_image_use_case(infer_image_use_case_arg, {"layout_kind": item}) is ImageUseCase.COVER_HERO


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


