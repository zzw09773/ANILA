"""LaTeX strip — Round 3 Patch I. Citation strip — Round 4 Patch Q."""
from __future__ import annotations

import pytest

from app.services.studio_text_normalizer import (
    strip_inline_citations,
    strip_latex,
)


def test_strip_latex_known_arrows():
    assert strip_latex("Observation $\\rightarrow$ Thought") == "Observation → Thought"
    assert strip_latex("A $\\to$ B $\\to$ C") == "A → B → C"
    assert strip_latex("$\\Rightarrow$ implies") == "⇒ implies"


@pytest.mark.parametrize(
    "strip_latex_arg, expected",
    [
        pytest.param("速度 $\\times$ 2", "速度 × 2", id='math_ops'),
        pytest.param("a $\\someweird$ b", "a \\someweird b", id='unknown_command_falls_back'),
        pytest.param("$\\alpha$ + $\\beta$", "α + β", id='greek_letters'),
    ],
)
def test_strip_latex(strip_latex_arg, expected):
    assert strip_latex(strip_latex_arg) == expected


def test_strip_latex_no_latex_passes_through():
    assert strip_latex("純中文沒有 LaTeX") == "純中文沒有 LaTeX"
    assert strip_latex("") == ""
    assert strip_latex(None) is None


def test_strip_latex_v3_slide12_regression():
    """Exact text from v3 slide 12 that triggered this patch."""
    input_text = "記錄 Observation $\\rightarrow$ Thought $\\rightarrow$ Action 軌跡"
    expected = "記錄 Observation → Thought → Action 軌跡"
    assert strip_latex(input_text) == expected


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


@pytest.mark.parametrize(
    "strip_inline_citations_arg, expected",
    [
        pytest.param("完成部署 (參 [5])", "完成部署", id='basic_half_width'),
        pytest.param("完成部署（參 [5]）", "完成部署", id='full_width_parens'),
        pytest.param("實作 (參 [12])", "實作", id='multi_digit'),
        pytest.param("成果 ( 參 [5] )", "成果", id='with_internal_whitespace'),
        # Some bullets cite multiple chunks consecutively.
        pytest.param("整合方案 (參 [5]) (參 [10])", "整合方案", id='multiple_at_end'),
        pytest.param("結論 (參考 [3])", "結論", id='alternative_wording'),
        pytest.param("全形（參 [3], [5]）", "全形", id='multi_number_full_width'),
        # Patch Q behaviour preserved.
        pytest.param("單一 (參 [5])", "單一", id='single_still_works'),
        # The existing 3x loop handles separate groups; CC handles within-group.
        pytest.param("分開 (參 [5]) (參 [10])", "分開", id='separate_groups_still_works'),
    ],
)
def test_strip_citation(strip_inline_citations_arg, expected):
    assert strip_inline_citations(strip_inline_citations_arg) == expected


@pytest.mark.parametrize(
    "txt, expected",
    [
        # Intra-text citations NOT stripped — too risky for false positives.
        pytest.param("如 (參 [5]) 所述，我們完成了部署", "如 (參 [5]) 所述，我們完成了部署", id='preserved'),
        # Multi-number must not make intra-text stripping over-eager.
        pytest.param("如 (參 [1], [8]) 所述，結論成立", "如 (參 [1], [8]) 所述，結論成立", id='multi_still_preserved'),
    ],
)
def test_strip_citation_intra_text(txt, expected):
    assert strip_inline_citations(txt) == expected


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


