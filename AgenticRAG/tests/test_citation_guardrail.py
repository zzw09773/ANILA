"""Tests for C2 — Citation guardrail."""

from __future__ import annotations

import json

import pytest

from agentic_rag.runtime.bridge.citation_guardrail import (
    CitationMissing,
    CitationReferences,
    check_final_answer,
    collect_references,
    enforce_citations,
)
from agentic_rag.runtime.framework.items import (
    MessageOutputItem,
    ToolResult,
    ToolResultItem,
)


def _tool_item(output_payload, *, name: str = "vector_search") -> ToolResultItem:
    if not isinstance(output_payload, str):
        output_str = json.dumps(output_payload)
    else:
        output_str = output_payload
    return ToolResultItem(
        result=ToolResult(call_id="c1", name=name, output=output_str),
        elapsed_seconds=0.01,
    )


# ── collect_references ───────────────────────────────────────────────


def test_collect_finds_chunks_in_results_array() -> None:
    item = _tool_item(
        {
            "results": [
                {
                    "chunk_id": "ch_001",
                    "document_id": 42,
                    "document_title": "RAG Handbook",
                    "heading_path": ["Intro", "What is RAG"],
                    "content": "...",
                },
                {
                    "chunk_id": "ch_002",
                    "document_id": 42,
                    "document_title": "RAG Handbook",
                    "heading_path": ["Intro", "Why RAG"],
                    "content": "...",
                },
            ]
        }
    )
    refs = collect_references([item])
    assert refs.chunk_ids == {"ch_001", "ch_002"}
    assert refs.document_ids == {"42"}
    assert refs.document_titles == {"RAG Handbook"}
    assert "Intro > What is RAG" in refs.heading_trails


def test_collect_skips_short_titles() -> None:
    """Titles under 4 chars are noisy — skip to avoid false positives."""
    item = _tool_item(
        {
            "results": [
                {"chunk_id": "x", "document_id": "d", "document_title": "RAG"}
            ]
        }
    )
    refs = collect_references([item])
    # "RAG" is 3 chars — should not enter document_titles.
    assert refs.document_titles == set()


def test_collect_skips_error_results() -> None:
    item = ToolResultItem(
        result=ToolResult(call_id="c1", name="vector_search", error="db down"),
        elapsed_seconds=0.0,
    )
    refs = collect_references([item])
    assert refs.is_empty


def test_collect_skips_non_tool_items() -> None:
    """MessageOutputItem (assistant text) shouldn't contribute citations."""
    refs = collect_references([MessageOutputItem()])
    assert refs.is_empty


def test_collect_handles_unparseable_output() -> None:
    item = _tool_item("just a plain string with no structure")
    refs = collect_references([item])
    assert refs.is_empty


# ── check_final_answer ───────────────────────────────────────────────


def test_check_passes_trivially_when_no_references() -> None:
    """Conversational turns with no retrieval shouldn't fail."""
    refs = CitationReferences()
    verdict = check_final_answer("any answer", refs)
    assert verdict.cited is True


def test_check_passes_when_chunk_id_in_answer() -> None:
    refs = CitationReferences(chunk_ids={"ch_001"})
    v = check_final_answer("according to ch_001 the answer is 42", refs)
    assert v.cited is True
    assert "ch_001" in v.matched


def test_check_passes_when_title_appears_case_insensitive() -> None:
    refs = CitationReferences(document_titles={"RAG Handbook"})
    v = check_final_answer("the rag handbook explains it well", refs)
    assert v.cited is True
    assert "RAG Handbook" in v.matched


def test_check_passes_on_heading_trail_segment_match() -> None:
    """A single sufficiently-long heading segment counts."""
    refs = CitationReferences(heading_trails={"Intro > What is RAG"})
    v = check_final_answer("the introduction to what is RAG explains...", refs)
    assert v.cited is True


def test_check_warn_mode_does_not_raise() -> None:
    refs = CitationReferences(chunk_ids={"ch_X"})
    v = check_final_answer("answer with no citation", refs, mode="warn")
    assert v.cited is False
    assert v.candidates == ["ch_X"]


def test_check_block_mode_raises_citation_missing() -> None:
    refs = CitationReferences(chunk_ids={"ch_X"})
    with pytest.raises(CitationMissing) as exc_info:
        check_final_answer("answer with no citation", refs, mode="block")
    assert "ch_X" in str(exc_info.value)


# ── enforce_citations end-to-end ─────────────────────────────────────


def test_enforce_citations_against_run_result() -> None:
    """The convenience helper walks RunResult.items and checks output."""

    class _FakeRunResult:
        items = [
            _tool_item(
                {
                    "results": [
                        {
                            "chunk_id": "ch_99",
                            "document_id": 7,
                            "document_title": "Local Models 101",
                        }
                    ]
                }
            )
        ]
        final_output = "see ch_99 — Local Models 101 covers this"

    verdict = enforce_citations(_FakeRunResult(), mode="block")
    assert verdict.cited is True


def test_enforce_block_mode_raises_when_answer_doesnt_cite() -> None:
    class _FakeRunResult:
        items = [
            _tool_item(
                {
                    "results": [
                        {"chunk_id": "ch_99", "document_id": 7, "document_title": "Local Models 101"}
                    ]
                }
            )
        ]
        final_output = "completely unrelated answer with no citation at all"

    with pytest.raises(CitationMissing):
        enforce_citations(_FakeRunResult(), mode="block")


def test_enforce_warn_mode_returns_failed_verdict() -> None:
    class _FakeRunResult:
        items = [
            _tool_item(
                {"results": [{"chunk_id": "ch_99", "document_id": 7, "document_title": "T1"}]}
            )
        ]
        final_output = "no citation here"

    verdict = enforce_citations(_FakeRunResult(), mode="warn")
    assert verdict.cited is False


def test_enforce_passes_when_no_retrieval_happened() -> None:
    """Conversational answer with no retrieval items → trivial pass."""

    class _FakeRunResult:
        items = []
        final_output = "Hello, how can I help?"

    verdict = enforce_citations(_FakeRunResult(), mode="block")
    assert verdict.cited is True
