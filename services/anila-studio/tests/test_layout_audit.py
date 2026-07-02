"""_audit_layout_distribution — detect V1/V2/V3/V4_CONTENT/V4_TITLE.

These are pure-logic unit tests over the deterministic audit step that
Studio Fix 1 inserts between schema validation and rendering. The
function is intentionally side-effect free (no LLM, no DB) so each test
just builds a SlidesSpec and asserts on the returned violation list.
"""
from __future__ import annotations

import pytest

from app.services.studio_layout import (
    LayoutViolation,
    _audit_layout_distribution,
    _should_rebalance,
)
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
        "palette": "navy_amber",
    })
    violations = _audit_layout_distribution(spec, chunks_text="")
    hard_v1 = [v for v in violations if v.kind == "V1"]
    assert len(hard_v1) == 1
    assert hard_v1[0].severity == "hard"


def test_v2_missing_stat_for_numeric_content():
    spec = SlidesSpec(**{
        "title": "T", "subtitle": "S",
        "slides": [_slide("slide")],
        "palette": "navy_amber",
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
        "palette": "navy_amber",
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
        "palette": "navy_amber",
    })
    violations = _audit_layout_distribution(spec, chunks_text="")
    v3 = [v for v in violations if v.kind == "V3"]
    assert len(v3) >= 1
    assert v3[0].severity == "soft"


def test_v4_enumeration_keyword_with_standard_3plus_bullets():
    # Title-keyword only (bullets a/b/c are NOT label-pattern) → V4_TITLE hint.
    spec = SlidesSpec(**{
        "title": "T", "subtitle": "S",
        "slides": [_slide("三大核心能力", bullets=["a", "b", "c"])],
        "palette": "navy_amber",
    })
    violations = _audit_layout_distribution(spec, chunks_text="")
    v4 = [v for v in violations if v.kind == "V4_TITLE"]
    assert len(v4) == 1
    assert 0 in v4[0].slide_indices
    assert v4[0].severity == "hint"


def test_v4_not_flagged_if_layout_not_standard():
    spec = SlidesSpec(**{
        "title": "T", "subtitle": "S",
        "slides": [_slide("三大核心能力", bullets=["a", "b", "c"], layout="icon_rows", icon_rows=[
            {"concept": "user", "heading": "h", "description": "d"},
            {"concept": "success", "heading": "h", "description": "d"},
            {"concept": "error", "heading": "h", "description": "d"},
        ])],
        "palette": "navy_amber",
    })
    violations = _audit_layout_distribution(spec, chunks_text="")
    assert not any(v.kind in ("V4_CONTENT", "V4_TITLE") for v in violations)


# Round 2 Patch E: lock in the v2 regression cases. The Round 1 keyword
# list missed common technical-deck patterns ('架構', '拓撲', '方案' ...),
# so slides like "Multi-Agent Supervisor 拓撲設計" weren't flagged as V4
# candidates and never got rebalanced. The expanded ~40-keyword tuple
# should now flag these.
@pytest.mark.parametrize("title,bullets,expected_v4", [
    ("Multi-Agent Supervisor 拓撲設計", ["a", "b", "c"], True),  # slide 13
    ("Agentic Workflow:從檢索到推理", ["x", "y", "z"], True),  # slide 8
    ("雙分支特徵融合解決方案", ["a", "b", "c"], True),  # slide 5 — "方案"
    ("封面標題", ["a", "b", "c"], False),  # no enumeration keyword
    ("拓撲", ["a", "b"], False),  # only 2 bullets, V4 requires >=3
    ("三大核心能力", ["a", "b", "c"], True),  # original case still works
    ("Generic Standard Slide", ["one", "two", "three"], False),  # no keyword
])
def test_v4_keyword_expansion(title, bullets, expected_v4):
    spec = SlidesSpec(**{
        "title": "T", "subtitle": "S",
        "slides": [_slide(title, bullets=bullets)],
        "palette": "navy_amber",
    })
    violations = _audit_layout_distribution(spec, chunks_text="")
    # These are title-keyword cases (bullets are not label-pattern), so they
    # surface as V4_TITLE hints, not V4_CONTENT.
    v4s = [v for v in violations if v.kind in ("V4_CONTENT", "V4_TITLE")]
    assert (len(v4s) > 0) == expected_v4


# Round 3 Patch H: V4 fires on bullet CONTENT pattern, not just title.
# Slides like 「執行摘要」 carry no enumeration keyword in the title but their
# bullets follow the textbook "label: description" shape — exactly the
# icon_rows-friendly material Round 2 was missing.
@pytest.mark.parametrize("title,bullets,expected_v4", [
    # ── Pure content-pattern hits (Round 3 new) ──
    ("執行摘要", [
        "高效能推理：掌握 TensorRT-LLM 並解決 C++ 對齊 Bug",
        "技術突破：實作法律 Agentic RAG",
        "合規治理：導入 ISO 42001",
        "架構演進：提出基於 DDD 的垂直切分架構",
    ], True),  # 4/4 label-pattern, no keyword
    ("底層推理引擎除錯實踐", [
        "問題：v1.2.0rc2 處理 Harmony 格式時導致服務崩潰",
        "根因:透過 Git Bisect 定位為 C++ 記憶體對齊瑕疵",
        "對策：降版至 v1.1.0rc5 並建立自動化回歸測試",
        "價值：證明具備原始碼級別除錯能力",
    ], True),

    # ── Title-keyword hits (Round 2 behaviour preserved) ──
    ("Multi-Agent Supervisor 拓撲設計", [
        "Supervisor 統籌 Worker",
        "Worker 執行專業任務",
        "高度模組化",
    ], True),

    # ── Negative cases ──
    ("執行摘要", [
        "今年完成了多項工作",
        "在推理上有顯著進展",
        "下一步將擴大研究範圍",
    ], False),  # no pattern, no keyword
    ("封面標題", ["a", "b", "c"], False),
    ("過程描述", ["經過調研後團隊決定採用 ReAct 框架"], False),  # only 1 bullet
])
def test_v4_content_pattern_and_keyword_paths(title, bullets, expected_v4):
    spec = SlidesSpec(**{
        "title": "T", "subtitle": "S",
        "slides": [_slide(title, bullets=bullets)],
        "palette": "navy_amber",
    })
    violations = _audit_layout_distribution(spec, chunks_text="")
    v4s = [v for v in violations if v.kind in ("V4_CONTENT", "V4_TITLE")]
    assert (len(v4s) > 0) == expected_v4


def test_v4_pattern_threshold_70_percent():
    """Bullets need ≥70% label-pattern match to trigger pure pattern path."""
    spec = SlidesSpec(**{
        "title": "T", "subtitle": "S",
        "slides": [_slide(
            "實作筆記",  # no V4 keyword
            bullets=[
                "前提：先 install 依賴",   # match
                "步驟：跑 init 指令",      # match
                "完成後就可以使用了",      # no match
            ],
        )],
        "palette": "navy_amber",
    })
    violations = _audit_layout_distribution(spec, chunks_text="")
    # 2/3 = 66% < 70% → no V4 (and title has no keyword either)
    assert not any(v.kind in ("V4_CONTENT", "V4_TITLE") for v in violations)


# ── Round 4 Patch R: image_focus disguise detection ──
#
# The LLM learned to dodge the V4 audit (which only checked
# layout_kind == "standard") by emitting layout_kind="image_focus" with
# image_kind="illustration" and no real image. The audit must catch
# this disguise too, while still exempting real diagrams (image_kind=
# "diagram" with diagram_dot) and real Phase 5 images (image_ref set).


def test_v4_audit_catches_illustration_disguise():
    """image_focus + illustration + no image_ref + 3+ bullets → flagged.

    The disguise: layout_kind=image_focus but image_kind=illustration with
    only image_prompt (no image_ref, no diagram_dot) — visually this still
    becomes a text-heavy paragraph block with an empty image frame.
    """
    spec = SlidesSpec(**{
        "title": "T", "subtitle": "S",
        "slides": [_slide(
            "敏捷開發的核心原則",
            layout="image_focus",
            bullets=[
                "個人與互動勝過流程與工具",
                "可用軟體勝過完整文件",
                "客戶協作勝過契約協商",
                "回應變更勝過遵循計畫",
            ],
            image_kind="illustration",
            image_prompt="four pillars representing the agile manifesto",
        )],
        "palette": "navy_amber",
    })
    violations = _audit_layout_distribution(spec, chunks_text="")
    # Disguise is a strong signal → classified as V4_CONTENT (actionable).
    v4 = [v for v in violations if v.kind == "V4_CONTENT"]
    assert len(v4) == 1
    assert 0 in v4[0].slide_indices
    assert "image_focus_disguise" in v4[0].detail.lower() or "disguise" in v4[0].detail.lower()


def test_v4_audit_exempts_real_diagram():
    """image_focus + diagram + diagram_dot present → NEVER flagged.

    A real Graphviz diagram is exactly the visual asset image_focus is
    designed for; the audit must leave it alone.
    """
    spec = SlidesSpec(**{
        "title": "敏捷開發的核心原則",
        "subtitle": "S",
        "slides": [_slide(
            "敏捷開發的核心原則",
            layout="image_focus",
            bullets=[
                "個人與互動勝過流程與工具",
                "可用軟體勝過完整文件",
                "客戶協作勝過契約協商",
                "回應變更勝過遵循計畫",
            ],
            image_kind="diagram",
            diagram_dot="digraph G { a -> b; b -> c; c -> d; }",
        )],
        "palette": "navy_amber",
    })
    violations = _audit_layout_distribution(spec, chunks_text="")
    assert not any(v.kind in ("V4_CONTENT", "V4_TITLE") for v in violations)


def test_v4_audit_exempts_real_image_ref():
    """image_focus + illustration + image_ref present → NEVER flagged.

    image_ref set means a Phase 5 ingestion image (or hydrated FLUX
    output) is bound to this slide — the frame will actually contain a
    real image, so the disguise check should not fire.
    """
    spec = SlidesSpec(**{
        "title": "T", "subtitle": "S",
        "slides": [_slide(
            "敏捷開發的核心原則",
            layout="image_focus",
            bullets=[
                "個人與互動勝過流程與工具",
                "可用軟體勝過完整文件",
                "客戶協作勝過契約協商",
                "回應變更勝過遵循計畫",
            ],
            image_kind="illustration",
            image_prompt="four pillars representing the agile manifesto",
            image_ref="flux_generated_abc123",
        )],
        "palette": "navy_amber",
    })
    violations = _audit_layout_distribution(spec, chunks_text="")
    assert not any(v.kind in ("V4_CONTENT", "V4_TITLE") for v in violations)


# ── Round 6 Patch V: split V4 into V4_CONTENT (strong) / V4_TITLE (hint) ──
#
# Production [H-DIAG] trail (Job j_de1d67dac..., 2026-05-20) proved the
# audit conflated two signals of very different strength:
#   - bullets ARE label:description  → mechanically convertible → rebalance
#   - title merely contains '架構/流程/設計' but bullets are flowing prose →
#     LLM correctly refuses → should NOT be counted as a failure.
# These tests lock in the split.


def test_v4_content_fires_on_pure_label_pattern():
    """Bullets all label:description, title has NO keyword → V4_CONTENT only."""
    spec = SlidesSpec(**{
        "title": "T", "subtitle": "S",
        "slides": [_slide(
            "TensorRT-LLM 崩潰問題分析",  # no enumeration keyword
            bullets=[
                "問題：v1.2.0rc2 處理 Harmony 格式時導致服務崩潰",
                "根因：透過 Git Bisect 定位為 C++ 記憶體對齊瑕疵",
                "對策：降版至 v1.1.0rc5 並建立自動化回歸測試",
                "價值：證明具備原始碼級別除錯能力",
            ],
        )],
        "palette": "navy_amber",
    })
    violations = _audit_layout_distribution(spec, chunks_text="")
    content = [v for v in violations if v.kind == "V4_CONTENT"]
    title = [v for v in violations if v.kind == "V4_TITLE"]
    assert len(content) == 1
    assert len(title) == 0
    assert content[0].severity == "soft"


def test_v4_title_fires_as_hint_only():
    """Title contains '架構' but bullets are flowing narrative → V4_TITLE hint.

    This is the slide-14 production case: forcing icon_rows here would mean
    fabricating headings, so it must be a non-actionable hint.
    """
    spec = SlidesSpec(**{
        "title": "T", "subtitle": "S",
        "slides": [_slide(
            "未來架構觀察與建議",  # '架構' keyword
            bullets=[
                "我們認為未來的系統會朝向更鬆耦合的方向演進",
                "在持續觀察產業趨勢後團隊決定逐步導入服務化",
                "下一步將擴大研究範圍並評估長期維運成本",
            ],
        )],
        "palette": "navy_amber",
    })
    violations = _audit_layout_distribution(spec, chunks_text="")
    content = [v for v in violations if v.kind == "V4_CONTENT"]
    title = [v for v in violations if v.kind == "V4_TITLE"]
    assert len(content) == 0
    assert len(title) == 1
    assert title[0].severity == "hint"


def test_should_rebalance_only_on_content_or_hard():
    """Pure V4_TITLE hint → no rebalance; a V4_CONTENT → rebalance."""
    hints_only = [
        LayoutViolation(kind="V4_TITLE", severity="hint", slide_indices=[14], detail=""),
    ]
    assert _should_rebalance(hints_only) is False

    with_content = [
        LayoutViolation(kind="V4_TITLE", severity="hint", slide_indices=[14], detail=""),
        LayoutViolation(kind="V4_CONTENT", severity="soft", slide_indices=[9], detail=""),
    ]
    assert _should_rebalance(with_content) is True


def test_v4_content_threshold_is_one():
    """A single V4_CONTENT is enough to trigger rebalance (threshold 2→1)."""
    violations = [
        LayoutViolation(kind="V4_CONTENT", severity="soft", slide_indices=[9], detail=""),
    ]
    assert _should_rebalance(violations) is True


def test_v4_both_signals_classified_as_content():
    """title-keyword AND bullet-pattern both fire → V4_CONTENT, not V4_TITLE.

    Content-pattern is the dominant kind (strong wins).
    """
    spec = SlidesSpec(**{
        "title": "T", "subtitle": "S",
        "slides": [_slide(
            "三大核心能力",  # enumeration keyword
            bullets=[
                "高效能推理：掌握 TensorRT-LLM",
                "技術突破：實作法律 Agentic RAG",
                "合規治理：導入 ISO 42001",
            ],
        )],
        "palette": "navy_amber",
    })
    violations = _audit_layout_distribution(spec, chunks_text="")
    kinds = [v.kind for v in violations]
    assert "V4_CONTENT" in kinds
    assert "V4_TITLE" not in kinds
