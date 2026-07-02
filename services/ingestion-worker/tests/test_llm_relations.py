"""Tests for the pure helpers of ``ingestion_worker.llm_relations`` (Phase 2 / B).

The asyncpg + httpx glue needs a live LLM + PG (not stood up here), so we test
the prompt builder and — the important part — the response parser /
anti-hallucination validator: fabricated dst ids, unknown relation types,
ungrounded evidence and malformed JSON must all be rejected.

Neutral regulation names throughout.
"""
from __future__ import annotations

import json

from ingestion_worker.llm_relations import (
    LlmEdge,
    build_relation_messages,
    parse_llm_relations,
)


SOURCE = "本細則依公司獎懲辦法訂定。為配合作業另補充員工差勤管理辦法之規定。"


# ── prompt builder ───────────────────────────────────────────────────────────
def test_build_messages_lists_candidates_and_truncates():
    msgs = build_relation_messages(
        "x" * 100, [(2, "公司獎懲辦法"), (3, "員工差勤管理辦法")], max_chars=10
    )
    assert msgs[0]["role"] == "system" and "JSON" in msgs[0]["content"]
    user = msgs[1]["content"]
    assert "id=2: 公司獎懲辦法" in user and "id=3: 員工差勤管理辦法" in user
    assert "x" * 10 in user and "x" * 11 not in user  # text truncated to max_chars


# ── parser: happy path ───────────────────────────────────────────────────────
def test_parse_valid_edges():
    content = json.dumps([
        {"dst_document_id": 2, "relation_type": "based_on",
         "evidence": "本細則依公司獎懲辦法訂定", "confidence": 0.9},
        {"dst_document_id": 3, "relation_type": "supplements",
         "evidence": "補充員工差勤管理辦法之規定", "confidence": 0.7},
    ])
    edges = parse_llm_relations(content, candidate_ids={2, 3}, source_text=SOURCE)
    assert edges == [
        LlmEdge(2, "based_on", "本細則依公司獎懲辦法訂定", 0.9),
        LlmEdge(3, "supplements", "補充員工差勤管理辦法之規定", 0.7),
    ]


def test_parse_strips_code_fence_and_prose():
    content = "好的,結果如下:\n```json\n[{\"dst_document_id\":2,\"relation_type\":\"cites\",\"evidence\":\"依公司獎懲辦法\",\"confidence\":0.8}]\n```"
    edges = parse_llm_relations(content, candidate_ids={2}, source_text=SOURCE)
    assert len(edges) == 1 and edges[0].dst_document_id == 2


# ── parser: anti-hallucination ───────────────────────────────────────────────
def test_parse_drops_fabricated_dst():
    # id 99 is not a candidate → dropped
    content = json.dumps([
        {"dst_document_id": 99, "relation_type": "based_on",
         "evidence": "本細則依公司獎懲辦法訂定", "confidence": 1.0}
    ])
    assert parse_llm_relations(content, candidate_ids={2, 3}, source_text=SOURCE) == []


def test_parse_drops_unknown_relation_type():
    content = json.dumps([
        {"dst_document_id": 2, "relation_type": "inspired_by",
         "evidence": "本細則依公司獎懲辦法訂定", "confidence": 1.0}
    ])
    assert parse_llm_relations(content, candidate_ids={2}, source_text=SOURCE) == []


def test_parse_drops_ungrounded_evidence():
    # evidence not present in the source text → hallucinated → dropped
    content = json.dumps([
        {"dst_document_id": 2, "relation_type": "based_on",
         "evidence": "這句話根本不在原文裡面亂掰的", "confidence": 1.0}
    ])
    assert parse_llm_relations(content, candidate_ids={2}, source_text=SOURCE) == []


def test_parse_evidence_grounding_tolerates_whitespace():
    # model re-spaced the quote — collapsed-whitespace match still grounds it
    content = json.dumps([
        {"dst_document_id": 2, "relation_type": "based_on",
         "evidence": "本細則依 公司獎懲辦法 訂定", "confidence": 0.6}
    ])
    edges = parse_llm_relations(content, candidate_ids={2}, source_text=SOURCE)
    assert len(edges) == 1


def test_parse_clamps_confidence_and_dedups():
    content = json.dumps([
        {"dst_document_id": 2, "relation_type": "based_on",
         "evidence": "本細則依公司獎懲辦法訂定", "confidence": 5},
        {"dst_document_id": 2, "relation_type": "based_on",
         "evidence": "本細則依公司獎懲辦法訂定", "confidence": 0.3},  # dup → ignored
    ])
    edges = parse_llm_relations(content, candidate_ids={2}, source_text=SOURCE)
    assert len(edges) == 1 and edges[0].confidence == 1.0  # clamped 5 → 1.0


def test_parse_malformed_json_returns_empty():
    assert parse_llm_relations("not json at all", candidate_ids={2}, source_text=SOURCE) == []
    assert parse_llm_relations("", candidate_ids={2}, source_text=SOURCE) == []


def test_parse_missing_evidence_dropped():
    content = json.dumps([
        {"dst_document_id": 2, "relation_type": "based_on", "confidence": 0.9}
    ])
    assert parse_llm_relations(content, candidate_ids={2}, source_text=SOURCE) == []
