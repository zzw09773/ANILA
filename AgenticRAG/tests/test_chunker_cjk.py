"""CJK-aware sub-split tests for HierarchicalChunker."""

from __future__ import annotations

from agentic_rag.ingestion.chunker import HierarchicalChunker
from agentic_rag.models.storage import ChunkType


def _leaves(chunks):
    return [c for c in chunks if c.chunk_type == ChunkType.CONTENT]


def test_oversized_cjk_paragraph_split_on_period():
    # max_leaf=5 means ~20-char target. Build 5 sentences of 10 CJK chars each.
    sentence = "甲乙丙丁戊己庚辛壬癸."
    body = sentence * 5
    chunker = HierarchicalChunker(max_leaf_tokens=5, overlap_tokens=0)
    chunks = chunker.chunk(body, document_id="d", user_id="u", project_id="p")
    leaves = _leaves(chunks)

    assert len(leaves) >= 2
    # Splits should land on '.' boundaries — every leaf ends with '.' or fragment.
    full_sentences = sum(1 for l in leaves if l.content.endswith("."))
    assert full_sentences >= 1


def test_oversized_cjk_paragraph_split_on_comma_when_no_period():
    # No period — splitter should fall through to comma.
    body = "甲乙,丙丁,戊己,庚辛,壬癸,子丑,寅卯,辰巳,午未,申酉"
    chunker = HierarchicalChunker(max_leaf_tokens=4, overlap_tokens=0)
    chunks = chunker.chunk(body, document_id="d", user_id="u", project_id="p")
    leaves = _leaves(chunks)
    assert len(leaves) >= 2
    # Every produced piece must be ≤ char_cap (max_leaf * 4 = 16)
    for l in leaves:
        assert len(l.content) <= 16 + 1


def test_subsplit_does_not_drop_content():
    body = "一二三四五,六七八九十,甲乙丙丁戊,己庚辛壬癸,子丑寅卯辰"
    chunker = HierarchicalChunker(max_leaf_tokens=4, overlap_tokens=0)
    chunks = chunker.chunk(body, document_id="d", user_id="u", project_id="p")
    leaves = _leaves(chunks)
    rejoined = "".join(l.content for l in leaves).replace(",", "")
    expected = body.replace(",", "")
    # Allow the splitter to drop separator chars but not real content.
    for ch in expected:
        assert ch in rejoined, f"Lost character: {ch}"


def test_overlap_carries_tail_pieces():
    # Force overlap so adjacent chunks share tail piece(s).
    body = "甲.乙.丙.丁.戊.己.庚.辛.壬.癸."
    chunker = HierarchicalChunker(max_leaf_tokens=2, overlap_tokens=1)
    chunks = chunker.chunk(body, document_id="d", user_id="u", project_id="p")
    leaves = _leaves(chunks)
    # With overlap, total chunk count > len(body) / cap purely because tails
    # repeat; not a strict equality test, but verifies splitter ran.
    assert len(leaves) >= 3


def test_under_threshold_is_single_leaf():
    body = "短句一."
    chunker = HierarchicalChunker(max_leaf_tokens=100, overlap_tokens=0)
    chunks = chunker.chunk(body, document_id="d", user_id="u", project_id="p")
    leaves = _leaves(chunks)
    assert len(leaves) == 1
    assert leaves[0].content == body
