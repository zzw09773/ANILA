"""Theme schema — Round 3 Patch L."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.studio import SlidesSpec, Slide


def _minimal_slides():
    return [Slide(title="s", bullets=["a"])]


def test_theme_resolves_from_legacy_palette():
    """Legacy 'palette=charcoal_minimal' alone should resolve to academic_paper."""
    spec = SlidesSpec(
        title="t",
        slides=_minimal_slides(),
        palette="charcoal_minimal",
    )
    assert spec.theme == "academic_paper"


def test_theme_explicit_overrides_palette():
    """Explicit theme wins over palette."""
    spec = SlidesSpec(
        title="t",
        slides=_minimal_slides(),
        palette="navy_amber",
        theme="warm_journal",
    )
    assert spec.theme == "warm_journal"


def test_default_palette_resolves_to_corporate_navy():
    """No palette, no theme → default."""
    spec = SlidesSpec(title="t", slides=_minimal_slides())
    assert spec.theme == "corporate_navy"


def test_executive_brief_theme_has_no_palette_alias():
    """executive_brief must be specified explicitly (no palette maps to it)."""
    spec = SlidesSpec(
        title="t",
        slides=_minimal_slides(),
        theme="executive_brief",
    )
    assert spec.theme == "executive_brief"
    # palette unchanged (still default navy_amber), theme explicit.
    assert spec.palette == "navy_amber"


def test_invalid_theme_rejected():
    with pytest.raises(ValidationError):
        SlidesSpec(
            title="t",
            slides=_minimal_slides(),
            theme="not_a_real_theme",
        )


def test_each_legacy_palette_maps_to_a_theme():
    """Every existing palette must resolve to some valid theme."""
    mappings = [
        ("navy_amber", "corporate_navy"),
        ("forest_moss", "warm_journal"),
        ("charcoal_minimal", "academic_paper"),
        ("coral_energy", "startup_pitch"),
    ]
    for palette, expected_theme in mappings:
        spec = SlidesSpec(title="t", slides=_minimal_slides(), palette=palette)
        assert spec.theme == expected_theme, f"{palette} → expected {expected_theme}, got {spec.theme}"
