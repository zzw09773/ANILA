"""anila_core.text.zh_normalizer — s2twp + 域內用語固定點驗收。"""
from __future__ import annotations

import pytest
from anila_core.text import normalize_report, normalize_zh_tw


@pytest.mark.parametrize(
    "normalize_zh_tw_arg, expected",
    [
        pytest.param("机关单位的视频质量", "機關單位的影片質量", id='simplified_to_tw_lexicon'),
        pytest.param("本系統採用先進的導彈與激光技術", "本系統採用先進的飛彈與雷射技術", id='defense_domain_terms_after_s2twp'),
    ],
)
def test_simplified_to_tw_lexicon_cases(normalize_zh_tw_arg, expected):
    # 质量→質量 via s2twp only（字形）；不得再被域內詞替換成「品質」
    assert normalize_zh_tw(normalize_zh_tw_arg) == expected


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("演算法與人工智慧與飛彈", id='already_correct_is_fixed_point'),
        pytest.param("MAPE 降低 88.73%、F1=0.92", id='numbers_units_latin_passthrough'),
    ],
)
def test_already_correct_is_fixed_point_cases(text):
    assert normalize_zh_tw(text) == text


def test_physics_mass_terms_unchanged():
    """質量 is mass in defense/engineering — never rewrite to 品質."""
    for text in ("質量守恆定律", "彈頭質量 500 公斤", "質量流率"):
        assert normalize_zh_tw(text) == text


def test_empty_and_none():
    assert normalize_zh_tw("") == ""
    assert normalize_zh_tw(None) is None
    assert normalize_report("") == ("", 0)
    assert normalize_report(None) == (None, 0)


def test_normalize_report_counts_changes():
    out, n = normalize_report("导弹")
    assert out == "飛彈"
    assert n > 0
    out2, n2 = normalize_report("飛彈")
    assert out2 == "飛彈"
    assert n2 == 0
