# -*- coding: utf-8 -*-
"""數字、金額、天數、條號只跟送進模型的片段比。模型不能自己宣告對得上。"""
from anila_core.api.router_server import merge_round_anila_meta
from anila_core.source_number_check import (
    SOURCE_MISMATCH_MARKER,
    check_answer_against_sources,
    reconcile_source_checks,
)

_LEAVE = "第三條 請假不得超過三十日，並發給新臺幣三萬元。"


def _values(result, kind=None):
    rows = result["unmatched"]
    if kind is not None:
        rows = [row for row in rows if row["kind"] == kind]
    return [row["value"] for row in rows]


def test_chinese_and_arabic_forms_match():
    result = check_answer_against_sources(
        "依第3條，以30日為限，補助 30,000元。",
        [_LEAVE],
    )
    assert result["checked"] is True
    assert result["mismatch"] is False
    assert result["unmatched"] == []
    assert "marker" not in result


def test_fullwidth_digits_match_chinese_days():
    result = check_answer_against_sources("不得超過３０日。", ["請假上限為三十日。"])
    assert result["mismatch"] is False


def test_missing_article_amount_and_days_are_flagged():
    result = check_answer_against_sources(
        "依第9條，給45日，補助50,000元。",
        [_LEAVE],
    )
    assert result["mismatch"] is True
    assert result["marker"] == SOURCE_MISMATCH_MARKER
    assert "條:9" in _values(result, "article")
    assert "45" in _values(result, "days")
    assert "50000" in _values(result, "amount")


def test_article_does_not_match_a_bare_number():
    """15日裡的 15 不是第15條。條號要整條對上。"""
    result = check_answer_against_sources("依第15條辦理。", ["應於15日內完成。"])
    assert _values(result, "article") == ["條:15"]


def test_article_suffix_is_part_of_the_identity():
    source = "第十五條之一 另定之。"
    bare = check_answer_against_sources("適用第15條。", [source])
    assert bare["mismatch"] is True
    suffixed = check_answer_against_sources("適用第15條之1。", [source])
    assert suffixed["mismatch"] is False


def test_day_count_can_match_a_number_that_appears_in_the_passage():
    result = check_answer_against_sources("給15日。", ["第十五條 程序從簡。"])
    assert result["mismatch"] is False


def test_citation_markers_and_list_numbers_are_not_claims():
    result = check_answer_against_sources(
        "1. 先申請\n2. 再核定\n見 [1]。",
        ["第三條 紙本不再受理。"],
    )
    assert result["claims"] == []
    assert result["mismatch"] is False


def test_fenced_code_is_not_checked():
    result = check_answer_against_sources(
        "條文如下。\n```\n第99條 給88日\n```\n",
        ["第三條 紙本不再受理。"],
    )
    assert result["mismatch"] is False


def test_protocol_lines_are_not_part_of_the_answer():
    result = check_answer_against_sources(
        "依第三條辦理。\nDISPATCH:regs:第99條給45日",
        [_LEAVE],
    )
    assert result["mismatch"] is False


def test_no_sources_does_not_flag():
    result = check_answer_against_sources("依第9條，給45日。", [])
    assert result["checked"] is False
    assert result["mismatch"] is False
    assert result["reason"] == "no_sources"
    assert "marker" not in result
    assert result["claims"]


def test_date_day_is_not_a_day_count():
    source = "本要點自2024年3月15日施行。"
    same = check_answer_against_sources("自2024年3月15日起施行。", [source])
    assert same["mismatch"] is False
    shifted = check_answer_against_sources("自2024年3月20日起施行。", [source])
    assert "20" in _values(shifted, "number")
    assert "15" not in _values(shifted)


def test_digit_by_digit_year_matches_arabic():
    result = check_answer_against_sources(
        "自民國一一三年施行。",
        ["自民國113年施行。"],
    )
    assert result["mismatch"] is False


def test_ten_thousand_scale():
    result = check_answer_against_sources("核發1.5萬元。", ["每次新臺幣一萬五千元。"])
    assert result["mismatch"] is False


def test_reconcile_uses_both_rounds_sources():
    first = check_answer_against_sources("給45日。", ["第三條 只有三十日。"])
    second = check_answer_against_sources("差勤另定。", ["另有45日的補休。"])
    assert first["mismatch"] is True
    merged = reconcile_source_checks(
        first,
        second,
        ["第三條 只有三十日。", "另有45日的補休。"],
    )
    assert merged["mismatch"] is False
    assert merged["checked"] is True


def test_reconcile_without_sources_does_not_flag():
    first = check_answer_against_sources("給45日。", [])
    second = check_answer_against_sources("再給10日。", None)
    merged = reconcile_source_checks(first, second, [])
    assert merged["checked"] is False
    assert merged["mismatch"] is False


def test_round_merge_keeps_citations_and_rechecks_numbers():
    """後一輪不能把前一輪來源裡已經有的數字標成對不上，也不能把對不上的蓋掉。"""
    cleared = merge_round_anila_meta(
        {
            "kb_state": "searched_hit",
            "kb_hits": [{"document_id": 21, "content": "第三條 另有45日補休。"}],
            "citations": [{"id": "kb:1", "title": "人事", "snippet": "45日"}],
            "source_check": check_answer_against_sources(
                "依第三條。",
                ["第三條 另有45日補休。"],
            ),
        },
        {
            "kb_state": "searched_hit",
            "kb_hits": [{"document_id": 34, "content": "差勤程序從簡。"}],
            "citations": [{"id": "kb:2", "title": "差勤", "snippet": "從簡"}],
            "source_check": check_answer_against_sources(
                "給45日。",
                ["差勤程序從簡。"],
            ),
        },
    )
    assert [row["id"] for row in cleared["citations"]] == ["kb:1", "kb:2"]
    assert cleared["source_check"]["checked"] is True
    assert cleared["source_check"]["mismatch"] is False

    kept = merge_round_anila_meta(
        {
            "kb_hits": [{"document_id": 1, "content": "第三條 請假三十日。"}],
            "citations": [{"id": "kb:1", "title": "人事", "snippet": "三十日"}],
            "source_check": check_answer_against_sources(
                "給45日。",
                ["第三條 請假三十日。"],
            ),
        },
        {
            "kb_hits": [{"document_id": 2, "content": "第五條 事前申請。"}],
            "citations": [{"id": "kb:2", "title": "差勤", "snippet": "第五條"}],
            "source_check": check_answer_against_sources(
                "依第五條。",
                ["第五條 事前申請。"],
            ),
        },
    )
    assert kept["source_check"]["mismatch"] is True
    assert "45" in [row["value"] for row in kept["source_check"]["unmatched"]]
