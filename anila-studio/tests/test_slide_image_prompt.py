"""Slide.image_prompt — new field for LLM-requested generated images."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.studio import Slide


def test_slide_accepts_image_prompt():
    s = Slide(
        title="Tank operations",
        bullets=["Mountain patrol", "Engagement protocol"],
        image_prompt="A military tank patrolling mountainous terrain, cinematic, photorealistic",
    )
    assert s.image_prompt is not None
    assert "tank" in s.image_prompt.lower()


def test_slide_image_prompt_defaults_to_none():
    s = Slide(title="X", bullets=["a"])
    assert s.image_prompt is None


def test_slide_can_have_both_ref_and_prompt():
    """Schema allows both — hydration logic decides priority."""
    s = Slide(
        title="X",
        bullets=["a"],
        image_ref="img-abc123",
        image_prompt="A backup illustration if ref fails",
    )
    assert s.image_ref == "img-abc123"
    assert s.image_prompt is not None


def test_slide_rejects_too_long_image_prompt():
    with pytest.raises(ValidationError):
        Slide(
            title="X",
            bullets=["a"],
            image_prompt="A" * 501,  # > max_length=500
        )
