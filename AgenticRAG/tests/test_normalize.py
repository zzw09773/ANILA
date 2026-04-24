"""Tests for normalize_zh — symmetric input normalization for ingest + query."""

from __future__ import annotations

from agentic_rag.ingestion.normalize import normalize_zh


def test_empty_string_passthrough():
    assert normalize_zh("") == ""


def test_idempotent():
    text = "臺北 ＴＡＩＰＥＩ，第 8 條"
    once = normalize_zh(text)
    twice = normalize_zh(once)
    assert once == twice


def test_fullwidth_ascii_to_halfwidth():
    assert normalize_zh("ＴＡＩＰＥＩ") == "TAIPEI"
    assert normalize_zh("ＡＢＣ１２３") == "ABC123"


def test_cjk_punctuation_normalized_to_ascii():
    # Mapping makes both forms searchable as the same canonical text.
    assert normalize_zh("臺北，台北") == "臺北,台北"
    assert normalize_zh("項目；其他") == "項目;其他"
    assert normalize_zh("姓名：王小明") == "姓名:王小明"
    assert normalize_zh("為何？") == "為何?"
    assert normalize_zh("好！") == "好!"


def test_strip_single_space_between_cjk():
    # PDF artifact — single ASCII space between two CJK glyphs is removed.
    assert normalize_zh("申 誡 條 件") == "申誡條件"
    assert normalize_zh("陸 海 空 軍 懲 罰 法") == "陸海空軍懲罰法"


def test_preserves_space_around_digits():
    # "第 8 條" has digits, so the strip rule (CJK-space-CJK) leaves it alone.
    assert normalize_zh("第 8 條") == "第 8 條"
    assert normalize_zh("第8條") == "第8條"


def test_preserves_space_around_ascii_words():
    assert normalize_zh("Hello 世界") == "Hello 世界"
    assert normalize_zh("世界 World") == "世界 World"


def test_collapses_multi_space():
    assert normalize_zh("hello    world") == "hello world"
    # Ideographic space (U+3000) collapsed too.
    assert normalize_zh("a　　b") == "a b"


def test_preserves_newlines():
    assert normalize_zh("para1\n\npara2") == "para1\n\npara2"
    assert normalize_zh("line1\nline2") == "line1\nline2"


def test_trailing_whitespace_stripped_per_line():
    assert normalize_zh("hello   \nworld   ") == "hello\nworld"


def test_query_and_document_match_after_normalize():
    doc = normalize_zh("姓名：王小明，職位：工程師")
    q1 = normalize_zh("姓名:王小明")
    q2 = normalize_zh("姓名：王小明")
    assert q1 in doc
    assert q2 in doc
    assert q1 == q2
