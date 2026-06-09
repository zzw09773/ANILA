"""Tests for ``anila_core.ingestion.relation_resolution`` — the pure
target-resolution policy shared by the worker and the CSP service.

No DB: we hand :func:`match_target` candidate lists directly and assert the
resolve / unresolved / ambiguous decision (design v2 §6 step 2-4). Fixtures use
NEUTRAL regulation names (generic, no real-world labels).
"""
from __future__ import annotations

from anila_core.ingestion.relation_resolution import (
    MatchResult,
    match_target,
    ref_title,
)


# ── ref_title ────────────────────────────────────────────────────────────────
def test_ref_title_name_only():
    assert ref_title("員工差勤管理辦法") == "員工差勤管理辦法"


def test_ref_title_strips_article():
    assert ref_title("員工差勤管理辦法 第3條") == "員工差勤管理辦法"
    assert ref_title("公司治理準則 第10條之一") == "公司治理準則"


def test_ref_title_empty():
    assert ref_title("") == ""
    assert ref_title(None) == ""


# ── exact match ──────────────────────────────────────────────────────────────
def test_exact_single_hit():
    r = match_target("員工差勤管理辦法", [(1, "員工差勤管理辦法"), (2, "公司資安規則")])
    assert r == MatchResult(1, False, (1,))
    assert r.resolved is True


def test_exact_no_hit_is_unresolved():
    r = match_target("不存在的辦法", [(1, "員工差勤管理辦法")])
    assert r.document_id is None
    assert r.ambiguous is False
    assert r.resolved is False


def test_exact_multiple_hits_is_ambiguous():
    # two documents share the same normalized title → never silently pick one
    r = match_target("作業規定", [(1, "作業規定"), (2, "作業規定"), (3, "其他辦法")])
    assert r.document_id is None
    assert r.ambiguous is True
    assert set(r.candidate_ids) == {1, 2}


def test_exact_excludes_self():
    # a document must not resolve a citation to itself
    r = match_target("母法", [(1, "母法")], exclude_doc_id=1)
    assert r.resolved is False
    assert r.ambiguous is False


# ── contains fallback ────────────────────────────────────────────────────────
def test_contains_when_no_exact():
    # cited short name is a substring of the longer official title
    r = match_target("資安規則", [(1, "公司資訊資安規則"), (2, "差勤管理辦法")])
    assert r == MatchResult(1, False, (1,))


def test_contains_reverse_direction():
    # the doc's title is a substring of the (longer) cited name
    r = match_target("公司資安規則施行細則", [(1, "公司資安規則")])
    assert r.document_id == 1


def test_contains_multiple_is_ambiguous():
    r = match_target("規則", [(1, "甲規則"), (2, "乙規則")])
    assert r.ambiguous is True
    assert set(r.candidate_ids) == {1, 2}


def test_exact_beats_contains():
    # exact (id 2) wins even though id 1 would also contain-match
    r = match_target("資安規則", [(1, "公司資安規則細則"), (2, "資安規則")])
    assert r.document_id == 2
    assert r.ambiguous is False


# ── edge cases ───────────────────────────────────────────────────────────────
def test_empty_target_name():
    assert match_target("", [(1, "員工差勤管理辦法")]).resolved is False


def test_empty_candidates():
    assert match_target("母法", []).resolved is False


def test_blank_candidate_title_ignored():
    r = match_target("母法", [(1, ""), (2, "母法")])
    assert r.document_id == 2
