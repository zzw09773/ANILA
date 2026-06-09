"""Tests for ``anila_core.ingestion.citation_extractor``.

Phase 1 of document-relations (see docs/ingestion/document-relations-design.md):
PURE text → list[Citation]. No DB, no resolution here — that lives in the
worker/CSP layer. We assert:

1. Chinese-numeral normalization (cjk_to_int): 〇/零/十/百/兩/廿 + arabic + full-width.
2. Title normalization (normalize_title): 書名號 / full-width / whitespace stripped.
3. Citation extraction: regulation name + article, relation_type classified from
   the cue verb, 第N條之一 / 範圍 / 項款目, multiple edges per sentence, no-citation → [].
4. ReDoS / perf guard: pathological long input returns quickly, evidence truncated.

Fixtures use NEUTRAL regulation names (generic, no real-world labels).
"""
from __future__ import annotations

import time

import pytest

from anila_core.ingestion.citation_extractor import (
    Citation,
    cjk_to_int,
    extract_citations,
    normalize_title,
)


# ── Chinese numerals ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "s,expected",
    [
        ("五", 5), ("十", 10), ("十一", 11), ("二十", 20), ("二十一", 21),
        ("一百", 100), ("一百零五", 105), ("一百二十三", 123),
        ("兩", 2), ("廿", 20), ("卅", 30), ("〇", 0), ("零", 0),
        ("5", 5), ("10", 10), ("１２", 12),  # full-width digits
    ],
)
def test_cjk_to_int(s, expected):
    assert cjk_to_int(s) == expected


def test_cjk_to_int_invalid():
    assert cjk_to_int("") is None
    assert cjk_to_int("abc") is None


# ── Title normalization ──────────────────────────────────────────────────────
def test_normalize_title_strips_brackets_and_space():
    assert normalize_title("「員工差勤管理辦法」") == "員工差勤管理辦法"
    assert normalize_title("《公司資安規則》") == "公司資安規則"
    assert normalize_title("員工 差勤 管理辦法") == "員工差勤管理辦法"


def test_normalize_title_fullwidth_digits():
    # full-width digits in a name normalize to half-width
    assert normalize_title("１０２年作業辦法") == "102年作業辦法"


# ── Extraction: relation_type classification ─────────────────────────────────
def _one(text):
    cites = extract_citations(text)
    assert len(cites) == 1, f"expected 1 citation, got {cites}"
    return cites[0]


def test_based_on():
    c = _one("本辦法依員工差勤管理辦法第五條訂定。")
    assert c.relation_type == "based_on"
    assert c.target_title == "員工差勤管理辦法"
    assert c.article == "第5條"
    assert c.target_ref == "員工差勤管理辦法 第5條"


def test_amends():
    c = _one("茲修正公司資安規則第三條。")
    assert c.relation_type == "amends"
    assert c.target_title == "公司資安規則"
    assert c.article == "第3條"


def test_supersedes():
    c = _one("廢止個人資料保護辦法。")
    assert c.relation_type == "supersedes"
    assert c.target_title == "個人資料保護辦法"
    assert c.article is None


def test_cites_with_article_variant():
    c = _one("相關事項準用公司治理準則第十條之一規定。")
    assert c.relation_type == "cites"
    assert c.article == "第10條之一"


def test_article_with_item_and_clause():
    c = _one("依採購作業規定第五條第二項第三款辦理。")
    assert c.relation_type == "based_on"
    assert c.article == "第5條第2項第3款"


def test_no_citation_returns_empty():
    assert extract_citations("這是一段完全沒有引用任何規章的描述文字。") == []


def test_multiple_citations_in_one_sentence():
    cites = extract_citations("本規定依資訊安全管理法第三條訂定,並修正資料保護辦法第五條。")
    by_type = {c.relation_type: c for c in cites}
    assert "based_on" in by_type and by_type["based_on"].target_title == "資訊安全管理法"
    assert "amends" in by_type and by_type["amends"].target_title == "資料保護辦法"


def test_article_range_preserved():
    c = _one("參照差勤管理辦法第三條至第八條。")
    assert c.relation_type == "cites"
    # range kept as a single textual article ref (Phase 1; expansion is later)
    assert "至" in c.article and c.article.startswith("第3條")


# ── Self-reference filtering (本法 / 本章 / 前項 …) ────────────────────────────
def test_self_reference_with_article_dropped():
    # "本法第五十七條規定" is the document referring to its OWN parent law inline,
    # not a citation of another document — must yield nothing.
    assert extract_citations("權責長官得依本法第五十七條規定辦理。") == []


def test_self_reference_plain_dropped():
    for txt in (
        "依本法規定辦理。",
        "依本章規定。",
        "依本節規定辦理。",
        "準用前項規定。",
        "依該辦法辦理。",
    ):
        assert extract_citations(txt) == [], txt


def test_article_separated_not_swallowed_into_name():
    # name ends in 規定 + a following 第N條 + trailing 規定 — the article must be
    # parsed into ``article``, never glued onto ``target_title``.
    c = _one("依資通安全管理規定第三條規定辦理。")
    assert c.target_title == "資通安全管理規定"
    assert c.article == "第3條"
    assert c.target_ref == "資通安全管理規定 第3條"


def test_real_name_starting_with_self_ref_char_kept():
    # "前瞻基礎建設特別條例" starts with 前 but 前+瞻 is NOT a self-ref token —
    # it is a real regulation and must still be extracted.
    c = _one("依前瞻基礎建設特別條例第三條辦理。")
    assert c.target_title == "前瞻基礎建設特別條例"
    assert c.article == "第3條"


def test_parent_law_reference_still_extracted():
    # the explicit parent-law citation in a 施行細則 is a real cross-doc edge.
    c = _one("本細則依公司獎懲辦法訂定。")
    assert c.relation_type == "based_on"
    assert c.target_title == "公司獎懲辦法"
    assert c.article is None


# ── Dedup + evidence ─────────────────────────────────────────────────────────
def test_dedup_same_target_same_type():
    cites = extract_citations(
        "依員工差勤管理辦法第五條。又依員工差勤管理辦法第五條再述。"
    )
    assert len(cites) == 1  # same (relation_type, target_ref) collapsed


def test_evidence_truncated():
    long_tail = "說明" * 1000
    c = _one(f"依員工差勤管理辦法第五條{long_tail}")
    assert len(c.evidence) <= 500


# ── ReDoS / perf guard ───────────────────────────────────────────────────────
def test_pathological_input_is_fast():
    blob = ("　 \t第第第條條條" * 20000)  # 160k chars of near-misses
    start = time.monotonic()
    out = extract_citations(blob)
    assert isinstance(out, list)
    assert time.monotonic() - start < 3.0  # precompiled linear regex must not hang


def test_input_length_capped():
    # extremely long input is truncated (max_chars) and still returns
    huge = "x" * 5_000_000 + "依員工差勤管理辦法第五條"
    out = extract_citations(huge, max_chars=10_000)
    assert isinstance(out, list)  # citation past the cap is simply not scanned
