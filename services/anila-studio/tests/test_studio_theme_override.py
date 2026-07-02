"""theme_override — API-side bypass of LLM theme selection (Round 3 Patch P).

Pydantic-level tests only: the field exists, validates against the closed
set of THEMES via Literal, and defaults to None. The runtime pipeline
override (spec.theme = payload.theme_override) is covered by integration
behaviour — if the field is on the request and SlidesSpec.theme accepts
the same Literal set (which test_studio_theme_schema.py verifies), the
single-line assignment in _run_pipeline is mechanical.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.studio import THEMES, GenerateSpecRequest


def test_theme_override_accepts_valid_theme():
    """A valid THEMES value passes Pydantic validation."""
    req = GenerateSpecRequest(
        collection_id=1,
        preset="經典報告結構",
        theme_override="warm_journal",
    )
    assert req.theme_override == "warm_journal"


def test_theme_override_rejects_invalid_theme():
    """Anything outside the closed THEMES set is rejected at request time."""
    with pytest.raises(ValidationError):
        GenerateSpecRequest(
            collection_id=1,
            preset="經典報告結構",
            theme_override="not_a_theme",
        )


def test_theme_override_defaults_to_none():
    """Omitting the field is allowed; default is None (no override)."""
    req = GenerateSpecRequest(
        collection_id=1,
        preset="經典報告結構",
    )
    assert req.theme_override is None


def test_theme_override_accepts_all_five_themes():
    """Every value in THEMES must be accepted — guards against drift between
    the schema's Literal and the THEMES tuple."""
    for theme in THEMES:
        req = GenerateSpecRequest(
            collection_id=1,
            preset="經典報告結構",
            theme_override=theme,
        )
        assert req.theme_override == theme
