"""Round 5 Patch U — deterministic title-keyword theme override.

LLM tone detection (Patch O) was non-deterministic at signal boundaries:
v4 picked warm_journal for '11月學習心得報告', v5 picked corporate_navy
on essentially the same content because chunks lean technical and the
only warm_journal signal was the '心得' in the title.

These tests pin the architectural correctness: title is the strongest
author-intent signal — when it contains an unambiguous theme keyword,
override the LLM's tone-based choice. Title with no match leaves the
LLM choice intact (opt-in overriding, not blanket replacement).
"""
from __future__ import annotations

import pytest

from app.services.studio_layout import _apply_theme_title_override
from app.schemas.studio import Slide, SlidesSpec


def _make_spec(title: str, theme: str = "corporate_navy") -> SlidesSpec:
    return SlidesSpec(
        title=title,
        theme=theme,
        slides=[Slide(title="t", bullets=["x"], layout_kind="standard")],
    )


@pytest.mark.parametrize("title,expected", [
    # ── warm_journal ──
    ("11月學習心得報告", "warm_journal"),
    ("Q3 工作反思紀錄", "warm_journal"),
    ("年度回顧與展望", "warm_journal"),
    ("實習感想分享", "warm_journal"),
    # ── academic_paper ──
    ("關於 LLM 的研討會論文摘要", "academic_paper"),
    ("Workshop 投稿稿件", "academic_paper"),
    # ── startup_pitch ──
    ("產品發表簡報", "startup_pitch"),
    ("Series A 募資簡報", "startup_pitch"),
    # ── executive_brief ──
    ("Q3 高層 review", "executive_brief"),
    ("半年檢討會議簡報", "executive_brief"),
])
def test_title_override_forces_theme(title: str, expected: str) -> None:
    """Title with high-confidence keyword should force target theme."""
    spec = _make_spec(title, theme="corporate_navy")
    result = _apply_theme_title_override(spec)
    assert result.theme == expected


def test_no_match_preserves_llm_choice() -> None:
    """Title without keyword leaves LLM's theme choice intact."""
    spec = _make_spec("CNC 鐵屑辨識技術規劃", theme="academic_paper")
    result = _apply_theme_title_override(spec)
    assert result.theme == "academic_paper"


def test_override_idempotent_when_theme_matches() -> None:
    """If LLM already chose the right theme, no change but no error."""
    spec = _make_spec("學習心得", theme="warm_journal")
    result = _apply_theme_title_override(spec)
    assert result.theme == "warm_journal"


def test_v5_regression() -> None:
    """Exact title from v5 that regressed. Must override to warm_journal."""
    spec = _make_spec("11月學習心得報告", theme="corporate_navy")
    result = _apply_theme_title_override(spec)
    assert result.theme == "warm_journal"


def test_empty_title_safe() -> None:
    """Empty / whitespace-only title is a no-op (SlidesSpec rejects truly
    empty titles upstream; this just guards the helper itself)."""
    # SlidesSpec.title has min_length=1 so we build a spec with a real
    # title then mutate to simulate the edge case.
    spec = _make_spec("placeholder", theme="corporate_navy")
    spec.title = "   "
    result = _apply_theme_title_override(spec)
    assert result.theme == "corporate_navy"


def test_first_match_wins() -> None:
    """When title contains keywords from multiple patterns, first wins."""
    spec = _make_spec("心得論文發表", theme="corporate_navy")
    result = _apply_theme_title_override(spec)
    assert result.theme == "warm_journal"
