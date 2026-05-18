"""Studio Fix 3 — Stat / Column min-length enforcement.

These tests guard the schema-side half of the saturation fix. Sister
tests in `test_studio_saturation_degrade.py` cover the pre-validation
auto-correction path that catches under-filled LLM output before it
hits these validators.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.studio import Column, Stat


def test_stat_requires_supporting() -> None:
    """`supporting` was previously Optional; Fix 3 makes it mandatory."""
    with pytest.raises(ValidationError):
        Stat(value="95%", label="準確率")  # type: ignore[call-arg]


def test_stat_supporting_min_length_20() -> None:
    """Reject filler strings like '太短' / '重要突破' that pad the slide
    with nothing meaningful."""
    with pytest.raises(ValidationError):
        Stat(value="95%", label="準確率", supporting="太短")


def test_stat_supporting_full_length_ok() -> None:
    s = Stat(
        value="95%",
        label="準確率",
        supporting="雙分支架構相比單分支基準的 78%，提升 17 個百分點",
    )
    assert s.supporting.startswith("雙分支")


def test_stat_baseline_optional() -> None:
    s = Stat(
        value="95%",
        label="準確率",
        supporting="這是一段滿足 20 個字元以上的支援敘述文字測試樣本",
        baseline="78%",
        baseline_label="單分支基準",
    )
    assert s.baseline == "78%"
    assert s.baseline_label == "單分支基準"


def test_column_requires_2_bullets() -> None:
    """Round 2 Patch C: schema floor dropped 3 → 2 so legitimate
    2-bullet technical comparisons (e.g. v2 slide 5 雙分支特徵融合)
    aren't penalised. 1-bullet columns still rejected."""
    with pytest.raises(ValidationError):
        Column(heading="優點", bullets=["a"])


def test_column_with_2_bullets_ok() -> None:
    """Round 2 Patch C: 2 bullets per column is now schema-valid."""
    c = Column(heading="優點", bullets=["a", "b"])
    assert len(c.bullets) == 2


def test_column_with_3_bullets_ok() -> None:
    c = Column(heading="優點", bullets=["a", "b", "c"])
    assert len(c.bullets) == 3
