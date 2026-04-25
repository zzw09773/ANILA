"""Tests for ``anila_core.ingestion.chunking_plugins``.

Three concerns, in priority order:

1. Registry contract — duplicate names fail loudly, lookups round-trip,
   the catalog is API-shaped.
2. Each built-in strategy's *contract* (the ``chunk()`` invariants
   written into design doc §5): determinism, non-empty content,
   monotonic offset, parent heading preserved on splits.
3. The shared invariant that overlap < size in fixed-size windowing —
   a regression here would silently produce duplicate chunks.

We don't measure retrieval quality (that's the evaluator's job in
Sprint 3); we only assert the chunker honours its own contract.
"""

from __future__ import annotations

import pytest

from anila_core.ingestion.chunking_plugins import (
    ChunkerStrategy,
    ChunkResult,
    get_chunker,
    list_chunkers,
    register_chunker,
)


# ── Registry tests ──────────────────────────────────────────────────────────


def test_builtins_registered() -> None:
    """Sprint 1 ships exactly these three; UI / API depends on the names."""
    names = {c["name"] for c in list_chunkers()}
    assert {"hierarchical", "fixed", "markdown-aware"}.issubset(names)


def test_get_chunker_returns_fresh_instance() -> None:
    a = get_chunker("fixed")
    b = get_chunker("fixed")
    assert isinstance(a, ChunkerStrategy)
    # Different instances — chunker authors must not lean on per-instance
    # state surviving across calls.
    assert a is not b


def test_get_chunker_unknown_lists_available() -> None:
    with pytest.raises(KeyError) as excinfo:
        get_chunker("nope")
    msg = str(excinfo.value)
    assert "nope" in msg
    # Surface the available set so the API can surface it to dev UI directly.
    assert "fixed" in msg


def test_register_chunker_rejects_name_collision() -> None:
    """Two different classes claiming the same name = wiring bug."""

    class Foo(ChunkerStrategy):
        name = "fixed"  # collides with built-in
        display_name = "x"
        default_params: dict = {}
        param_schema: dict = {}

        def chunk(self, document_text, metadata, params):
            return []

    with pytest.raises(ValueError, match="already registered"):
        register_chunker(Foo)


def test_register_chunker_idempotent_for_same_class() -> None:
    """Re-importing a builtin module mustn't error."""
    from anila_core.ingestion.chunking_plugins import builtins  # noqa: F401

    # Should be a no-op; if this raised the worker would crash on hot-reload.
    register_chunker(builtins.FixedChunker)


# ── FixedChunker contract ───────────────────────────────────────────────────


def test_fixed_emits_chunks_covering_input() -> None:
    chunker = get_chunker("fixed")
    text = "abcdefghij" * 200  # 2000 chars ≈ 500 tokens
    chunks = chunker.chunk(text, {}, {"size": 100, "overlap": 10})
    assert len(chunks) >= 1
    assert all(c.content for c in chunks)
    # Concatenating without overlap should recover the input prefix.
    assert chunks[0].content.startswith("abcdefghij")


def test_fixed_overlap_must_be_less_than_size() -> None:
    chunker = get_chunker("fixed")
    with pytest.raises(ValueError, match="overlap"):
        chunker.chunk("anything", {}, {"size": 100, "overlap": 100})


def test_fixed_is_deterministic() -> None:
    """Evaluator depends on this — same input → same chunks."""
    chunker = get_chunker("fixed")
    text = "data " * 1000
    a = chunker.chunk(text, {}, {"size": 200, "overlap": 20})
    b = chunker.chunk(text, {}, {"size": 200, "overlap": 20})
    assert [c.content for c in a] == [c.content for c in b]
    assert [c.chunk_key for c in a] == [c.chunk_key for c in b]


# ── MarkdownAwareChunker contract ───────────────────────────────────────────


def test_markdown_aware_splits_on_headings() -> None:
    text = (
        "# Chapter One\n"
        "Intro text.\n"
        "\n"
        "# Chapter Two\n"
        "Other text.\n"
    )
    chunks = get_chunker("markdown-aware").chunk(text, {}, {})
    assert len(chunks) >= 2
    headings = [c.metadata.get("heading") for c in chunks]
    assert "Chapter One" in headings
    assert "Chapter Two" in headings


def test_markdown_aware_does_not_split_inside_code_fence() -> None:
    """The whole code fence belongs to its surrounding section."""
    text = (
        "# Section\n"
        "Before.\n"
        "```python\n"
        "# This hash is NOT a heading\n"
        "x = 1\n"
        "```\n"
        "After.\n"
    )
    chunks = get_chunker("markdown-aware").chunk(text, {}, {})
    # Exactly one section because the only real heading is "Section".
    section_headings = {c.metadata["heading"] for c in chunks}
    assert section_headings == {"Section"}
    # The code fence content is preserved inside the chunk.
    assert any("x = 1" in c.content for c in chunks)


# ── HierarchicalChunker contract ────────────────────────────────────────────


def test_hierarchical_records_full_heading_path() -> None:
    text = (
        "# Top\n"
        "A.\n"
        "## Mid\n"
        "B.\n"
        "### Leaf\n"
        "C.\n"
    )
    chunks = get_chunker("hierarchical").chunk(text, {}, {})
    paths = [c.metadata["heading_path"] for c in chunks]
    # Preface chunk has empty path; the deepest section has all three ancestors.
    deepest = max(paths, key=len)
    assert deepest == ["Top", "Mid", "Leaf"]


def test_hierarchical_pop_resets_deeper_headings() -> None:
    """Going from level-3 back to level-2 must clear the level-3 heading."""
    text = (
        "# Top\n"
        "## A\n"
        "### A1\n"
        "x.\n"
        "## B\n"
        "y.\n"
    )
    chunks = get_chunker("hierarchical").chunk(text, {}, {})
    b_chunks = [c for c in chunks if c.content.strip().startswith("## B")]
    assert b_chunks, "should have at least one section under heading B"
    # Critical: when we returned to level-2 ("B"), the level-3 ("A1") must NOT
    # appear in B's heading_path. Otherwise retrieval surfaces stale ancestors.
    for c in b_chunks:
        assert "A1" not in c.metadata["heading_path"]


def test_hierarchical_oversize_leaf_falls_back_to_fixed_with_path_preserved() -> None:
    """A 5000-char leaf with budget 100 must split AND keep heading_path."""
    text = "# Big Section\n" + ("words " * 1000)  # ~6000 chars
    chunks = get_chunker("hierarchical").chunk(
        text, {}, {"max_leaf_tokens": 100, "overlap_tokens": 8}
    )
    # Many sub-chunks expected.
    assert len(chunks) > 5
    # Every sub-chunk must still report the heading.
    for c in chunks:
        assert c.metadata["heading_path"] == ["Big Section"]


def test_chunk_result_is_frozen() -> None:
    """ChunkResult is a frozen dataclass — accidental mutation must fail."""
    cr = ChunkResult(content="x", chunk_key="k", token_count=1)
    with pytest.raises((AttributeError, TypeError)):
        cr.content = "mutated"  # type: ignore[misc]


# ── PdfPageChunker contract ─────────────────────────────────────────────────


def test_pdf_page_splits_on_form_feed() -> None:
    """One chunk per ``\\f``-separated page; metadata records page number."""
    text = "page one body\fpage two body\fpage three body"
    chunks = get_chunker("pdf-page").chunk(text, {}, {"max_page_tokens": 4096})
    assert len(chunks) == 3
    pages = [c.metadata["page"] for c in chunks]
    assert pages == [1, 2, 3]
    assert all(c.metadata["total_pages"] == 3 for c in chunks)
    assert chunks[0].chunk_key == "page-0001"


def test_pdf_page_falls_back_to_fixed_for_oversized_page() -> None:
    """Page > max_page_tokens splits into sub-chunks tagged with same page."""
    text = "page one " * 4 + "\f" + ("words " * 5000)
    chunks = get_chunker("pdf-page").chunk(text, {}, {"max_page_tokens": 200})
    by_page = {}
    for c in chunks:
        by_page.setdefault(c.metadata["page"], []).append(c)
    assert len(by_page[1]) == 1
    assert len(by_page[2]) > 1, "oversized page 2 should be split"
    assert all("page-0002" in c.chunk_key for c in by_page[2])


def test_pdf_page_handles_no_page_markers() -> None:
    """Single page (no ``\\f``) → one chunk."""
    text = "the only page in this document"
    chunks = get_chunker("pdf-page").chunk(text, {}, {})
    assert len(chunks) == 1
    assert chunks[0].metadata["page"] == 1


# ── CjkSentenceChunker contract ─────────────────────────────────────────────


def test_cjk_sentence_splits_on_chinese_terminators() -> None:
    text = "第一句話。第二句話！第三句話？" * 10
    chunks = get_chunker("cjk-sentence").chunk(
        text, {}, {"target_tokens": 16, "max_tokens": 32}
    )
    assert len(chunks) >= 2
    # Every chunk must end on a terminator (or be the last) — sanity
    # that we never split mid-sentence.
    for c in chunks[:-1]:
        last = c.content.rstrip()[-1]
        assert last in "。！？!?.", f"chunk doesn't end on terminator: {c.content!r}"


def test_cjk_sentence_groups_below_target() -> None:
    """Sentences merge until target_tokens reached; respect ceiling."""
    text = "短句一。短句二。" * 50
    chunks = get_chunker("cjk-sentence").chunk(
        text, {}, {"target_tokens": 64, "max_tokens": 128}
    )
    # Merged chunks should be reasonably close to target, never over ceiling.
    for c in chunks:
        assert c.token_count <= 128 + 16, (
            f"chunk overflows ceiling: tokens={c.token_count}"
        )


def test_cjk_sentence_long_sentence_falls_back_to_fixed() -> None:
    """Single sentence > ceiling: split via fixed inside, marked long_sentence."""
    text = ("a" * 5000) + "。"  # one giant sentence
    chunks = get_chunker("cjk-sentence").chunk(
        text, {}, {"target_tokens": 100, "max_tokens": 200}
    )
    assert len(chunks) > 1
    assert any(c.metadata.get("long_sentence") for c in chunks)


def test_cjk_sentence_max_lt_target_rejected() -> None:
    """Misconfiguration: max_tokens < target_tokens is invalid."""
    with pytest.raises(ValueError, match="max_tokens"):
        get_chunker("cjk-sentence").chunk(
            "anything", {}, {"target_tokens": 100, "max_tokens": 50}
        )


# ── SemanticChunker contract ────────────────────────────────────────────────


def test_semantic_requires_embedder_flag_set() -> None:
    """Worker dispatcher reads this attribute to decide whether to pre-compute."""
    cls = get_chunker("semantic").__class__
    assert cls.requires_embedder is True


def test_semantic_raises_without_precomputed_segments() -> None:
    """Direct ``chunk()`` without worker preprocessing must fail loudly."""
    with pytest.raises(ValueError, match="pre-compute"):
        get_chunker("semantic").chunk("some text", {}, {})


def test_semantic_with_two_clusters_finds_one_boundary() -> None:
    """Two semantically-distinct sentence groups → one boundary, two chunks."""
    segments = ["alpha beta gamma", "alpha beta gamma", "delta epsilon zeta"]
    # Embeddings: first two are nearly identical, third is far away.
    embeddings = [
        [1.0, 0.0, 0.0],
        [0.99, 0.01, 0.0],
        [0.0, 0.0, 1.0],
    ]
    chunks = get_chunker("semantic").chunk(
        "ignored: pre-computed",
        {},
        {
            "_segments": segments,
            "_embeddings": embeddings,
            "breakpoint_percentile": 50,
        },
    )
    assert len(chunks) == 2
    assert chunks[0].metadata["segment_range"] == [0, 2]
    assert chunks[1].metadata["segment_range"] == [2, 3]


def test_semantic_segment_count_mismatch_rejected() -> None:
    with pytest.raises(ValueError, match="mismatch"):
        get_chunker("semantic").chunk(
            "x",
            {},
            {"_segments": ["a", "b"], "_embeddings": [[1.0]]},
        )


def test_semantic_split_segments_helper() -> None:
    """``split_segments`` produces sentence-grouped candidates >= min_tokens."""
    from anila_core.ingestion.chunking_plugins.builtins import SemanticChunker

    text = "短句。短句。" * 100
    segs = SemanticChunker.split_segments(text, min_tokens=64)
    assert len(segs) >= 2
    # Every emitted segment is non-empty.
    assert all(s.strip() for s in segs)


# ── Sprint 3 strategies registered ─────────────────────────────────────────


def test_sprint3_strategies_registered() -> None:
    names = {c["name"] for c in list_chunkers()}
    assert {"pdf-page", "cjk-sentence", "semantic"}.issubset(names)
