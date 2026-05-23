"""LaTeX strip — Round 3 Patch I. Citation strip — Round 4 Patch Q."""
from __future__ import annotations

import pytest

from app.services.studio_text_normalizer import (
    normalize_spec,
    strip_bullet_prefix,
    strip_inline_citations,
    strip_latex,
)
from app.schemas.studio import Slide, SlidesSpec


def test_strip_bullet_prefix_black_circle():
    assert strip_bullet_prefix("● Advanced RAG：加入 Reranking") == "Advanced RAG：加入 Reranking"


def test_strip_bullet_prefix_variants():
    # 各種 LLM 會 inject 的 marker 都該被清掉
    assert strip_bullet_prefix("• 第一條") == "第一條"
    assert strip_bullet_prefix("▪ 黑方塊") == "黑方塊"
    assert strip_bullet_prefix("◆ 黑菱形") == "黑菱形"
    assert strip_bullet_prefix("* markdown asterisk") == "markdown asterisk"
    assert strip_bullet_prefix("- markdown dash 後文") == "markdown dash 後文"
    assert strip_bullet_prefix("·  middle dot") == "middle dot"


def test_strip_bullet_prefix_idempotent():
    # 已清過再清不該變
    out = strip_bullet_prefix("Advanced RAG：加入 Reranking")
    assert strip_bullet_prefix(out) == "Advanced RAG：加入 Reranking"


def test_strip_bullet_prefix_only_strips_once():
    # 兩層 marker 連發只剝一層,避免吃掉中文 enum 的真實連字
    assert strip_bullet_prefix("● ● 雙層") == "● 雙層"


def test_strip_bullet_prefix_preserves_chinese_dash_enum():
    # "一-加密" 是中文枚舉(非 list marker),不該剝
    assert strip_bullet_prefix("一-加密") == "一-加密"


def test_strip_bullet_prefix_no_marker_passthrough():
    assert strip_bullet_prefix("純文字") == "純文字"
    assert strip_bullet_prefix("") == ""
    assert strip_bullet_prefix(None) is None


def test_normalize_spec_strips_bullet_prefix_through_pipeline():
    """整支 pipeline:bullet 內的 ● + $\\nightarrow$ 都該乾淨。"""
    spec = SlidesSpec(
        title="t",
        slides=[
            Slide(
                title="x",
                bullets=[
                    "Naive RAG：線性",
                    "● Advanced RAG：加入 $\nightarrow$ Reranking",
                ],
                layout_kind="standard",
            )
        ],
    )
    out = normalize_spec(spec)
    assert out.slides[0].bullets[0] == "Naive RAG：線性"
    # ● 被剝、$\nightarrow$ 被 latex strip
    assert out.slides[0].bullets[1] == "Advanced RAG：加入 → Reranking"


def test_strip_latex_known_arrows():
    assert strip_latex("Observation $\\rightarrow$ Thought") == "Observation → Thought"
    assert strip_latex("A $\\to$ B $\\to$ C") == "A → B → C"
    assert strip_latex("$\\Rightarrow$ implies") == "⇒ implies"


def test_strip_latex_math_ops():
    assert strip_latex("速度 $\\times$ 2") == "速度 × 2"


def test_strip_latex_unknown_command_falls_back():
    # Unknown command → generic regex strips dollar wrappers, keeps inner.
    assert strip_latex("a $\\someweird$ b") == "a \\someweird b"


def test_strip_latex_no_latex_passes_through():
    assert strip_latex("純中文沒有 LaTeX") == "純中文沒有 LaTeX"
    assert strip_latex("") == ""
    assert strip_latex(None) is None


def test_strip_latex_v3_slide12_regression():
    """Exact text from v3 slide 12 that triggered this patch."""
    input_text = "記錄 Observation $\\rightarrow$ Thought $\\rightarrow$ Action 軌跡"
    expected = "記錄 Observation → Thought → Action 軌跡"
    assert strip_latex(input_text) == expected


def test_strip_latex_greek_letters():
    assert strip_latex("$\\alpha$ + $\\beta$") == "α + β"


def test_strip_latex_json_eaten_carriage_return():
    """Round 4 regression for v4 slide 11. JSON parser turns \\r into CR
    before strip_latex sees it."""
    broken = "失敗 " + "$" + "\r" + "ightarrow$" + " 思考"
    assert strip_latex(broken) == "失敗 → 思考"


def test_strip_latex_json_eaten_tab():
    broken = "A " + "$" + "\t" + "ightarrow$" + " B"
    assert strip_latex(broken) == "A → B"


def test_strip_latex_json_eaten_linefeed():
    broken = "A " + "$" + "\n" + "ightarrow$" + " B"
    assert strip_latex(broken) == "A → B"


def test_strip_latex_handles_unclosed_broken_variant():
    """LLM truncates mid-LaTeX — no closing $."""
    broken = "失敗 " + "$" + "\r" + "ightarrow" + " 後續"
    assert strip_latex(broken) == "失敗 → 後續"


def test_strip_latex_literal_path_still_works():
    """Patch I behaviour preserved for non-JSON paths (raw Python strings)."""
    text = r"Observation $\rightarrow$ Thought"
    assert strip_latex(text) == "Observation → Thought"


def test_strip_latex_json_eaten_theta():
    """\\theta in JSON → $<TAB>heta$ after parse."""
    broken = "誤差 " + "$" + "\t" + "heta$"
    assert strip_latex(broken) == "誤差 θ"


# ---------------------------------------------------------------------------
# Round 4 Patch Q — RAG citation strip from end of slide text.
# ---------------------------------------------------------------------------


def test_strip_citation_basic_half_width():
    assert strip_inline_citations("完成部署 (參 [5])") == "完成部署"


def test_strip_citation_full_width_parens():
    assert strip_inline_citations("完成部署（參 [5]）") == "完成部署"


def test_strip_citation_multi_digit():
    assert strip_inline_citations("實作 (參 [12])") == "實作"


def test_strip_citation_with_internal_whitespace():
    assert strip_inline_citations("成果 ( 參 [5] )") == "成果"


def test_strip_citation_multiple_at_end():
    """Some bullets cite multiple chunks consecutively."""
    assert strip_inline_citations("整合方案 (參 [5]) (參 [10])") == "整合方案"


def test_strip_citation_alternative_wording():
    assert strip_inline_citations("結論 (參考 [3])") == "結論"


def test_strip_citation_intra_text_preserved():
    """Intra-text citations NOT stripped — too risky for false positives."""
    txt = "如 (參 [5]) 所述，我們完成了部署"
    assert strip_inline_citations(txt) == "如 (參 [5]) 所述，我們完成了部署"


def test_strip_citation_no_citation_passes_through():
    assert strip_inline_citations("純粹陳述沒有引用") == "純粹陳述沒有引用"
    assert strip_inline_citations("") == ""
    assert strip_inline_citations(None) is None


def test_strip_citation_v4_journal_regression():
    """Exact bullet text from v4 slide 2."""
    input_text = "完成 gpt-oss-20b 與 NV-Embed-v2 之容器化部署，針對 GH200 進行記憶體調優 (參 [5])"
    expected = "完成 gpt-oss-20b 與 NV-Embed-v2 之容器化部署，針對 GH200 進行記憶體調優"
    assert strip_inline_citations(input_text) == expected


def test_strip_citation_multi_number_single_group():
    """Round CC: '(參 [1], [8])' — comma-separated numbers in ONE group."""
    assert strip_inline_citations("外資賣超 (參 [1], [8])") == "外資賣超"
    assert strip_inline_citations("三重 (參 [1], [8], [12])") == "三重"


def test_strip_citation_multi_number_full_width():
    assert strip_inline_citations("全形（參 [3], [5]）") == "全形"


def test_strip_citation_single_still_works():
    """Patch Q behaviour preserved."""
    assert strip_inline_citations("單一 (參 [5])") == "單一"


def test_strip_citation_separate_groups_still_works():
    """The existing 3x loop handles separate groups; CC handles within-group."""
    assert strip_inline_citations("分開 (參 [5]) (參 [10])") == "分開"


def test_strip_citation_intra_text_multi_still_preserved():
    """Multi-number must not make intra-text stripping over-eager."""
    txt = "如 (參 [1], [8]) 所述，結論成立"
    assert strip_inline_citations(txt) == "如 (參 [1], [8]) 所述，結論成立"
