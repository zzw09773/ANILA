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
