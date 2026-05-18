"""_audit_layout_distribution — detect V1/V2/V3/V4 violations.

These are pure-logic unit tests over the deterministic audit step that
Studio Fix 1 inserts between schema validation and rendering. The
function is intentionally side-effect free (no LLM, no DB) so each test
just builds a SlidesSpec and asserts on the returned violation list.
"""
from __future__ import annotations

from app.api.studio import _audit_layout_distribution
from app.schemas.studio import SlidesSpec


def _slide(title, layout="standard", bullets=None, **kw):
    """Build a minimal slide dict suitable for SlidesSpec construction.

    Keeps the test bodies readable — every slide we don't care about for
    the rule under test gets two cheap bullets and the default layout.
    """
    return {
        "title": title,
        "bullets": bullets or ["a", "b"],
        "layout_kind": layout,
        **kw,
    }


def test_v1_standard_over_60pct():
    # 8 standard + 2 icon_rows = 80% standard → V1 hard violation.
    spec = SlidesSpec(**{
        "title": "T", "subtitle": "S",
        "slides": [_slide(f"slide-{i}") for i in range(8)] +
                  [_slide(f"slide-x-{i}", layout="icon_rows", icon_rows=[
                      {"concept": "user", "heading": "h", "description": "d"},
                      {"concept": "success", "heading": "h", "description": "d"},
                      {"concept": "error", "heading": "h", "description": "d"},
                  ]) for i in range(2)],
        "theme": "navy_amber",
    })
    violations = _audit_layout_distribution(spec, chunks_text="")
    hard_v1 = [v for v in violations if v.kind == "V1"]
    assert len(hard_v1) == 1
    assert hard_v1[0].severity == "hard"


def test_v2_missing_stat_for_numeric_content():
    spec = SlidesSpec(**{
        "title": "T", "subtitle": "S",
        "slides": [_slide("slide")],
        "theme": "navy_amber",
    })
    chunks = "model achieved 95% accuracy on F1-score N=2400"
    violations = _audit_layout_distribution(spec, chunks_text=chunks)
    hard_v2 = [v for v in violations if v.kind == "V2"]
    assert len(hard_v2) == 1


def test_v2_not_flagged_if_stat_present():
    spec = SlidesSpec(**{
        "title": "T", "subtitle": "S",
        "slides": [
            _slide("a"),
            _slide("b", layout="stat_callout", stat={
                "value": "95%",
                "label": "準確率",
                "supporting": "雙分支架構相比單分支基準的 78%,N=2400",
            }),
        ],
        "theme": "navy_amber",
    })
    violations = _audit_layout_distribution(spec, chunks_text="95% F1-score")
    assert not any(v.kind == "V2" for v in violations)


def test_v3_three_consecutive_standard():
    spec = SlidesSpec(**{
        "title": "T", "subtitle": "S",
        "slides": [_slide(f"s{i}") for i in range(3)] + [
            _slide("non-standard", layout="quote", quote={
                "text": "a quote", "attribution": "someone",
            }),
        ],
        "theme": "navy_amber",
    })
    violations = _audit_layout_distribution(spec, chunks_text="")
    v3 = [v for v in violations if v.kind == "V3"]
    assert len(v3) >= 1
    assert v3[0].severity == "soft"


def test_v4_enumeration_keyword_with_standard_3plus_bullets():
    spec = SlidesSpec(**{
        "title": "T", "subtitle": "S",
        "slides": [_slide("三大核心能力", bullets=["a", "b", "c"])],
        "theme": "navy_amber",
    })
    violations = _audit_layout_distribution(spec, chunks_text="")
    v4 = [v for v in violations if v.kind == "V4"]
    assert len(v4) == 1
    assert 0 in v4[0].slide_indices


def test_v4_not_flagged_if_layout_not_standard():
    spec = SlidesSpec(**{
        "title": "T", "subtitle": "S",
        "slides": [_slide("三大核心能力", bullets=["a", "b", "c"], layout="icon_rows", icon_rows=[
            {"concept": "user", "heading": "h", "description": "d"},
            {"concept": "success", "heading": "h", "description": "d"},
            {"concept": "error", "heading": "h", "description": "d"},
        ])],
        "theme": "navy_amber",
    })
    violations = _audit_layout_distribution(spec, chunks_text="")
    assert not any(v.kind == "V4" for v in violations)
