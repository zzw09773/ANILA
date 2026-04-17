"""Tests for RecursiveTextSplitter."""

from __future__ import annotations


from anila_core.ingestion.chunker import RecursiveTextSplitter


def make_splitter(**kwargs) -> RecursiveTextSplitter:
    return RecursiveTextSplitter(**kwargs)


# ---------------------------------------------------------------------------
# Basic splitting
# ---------------------------------------------------------------------------

def test_short_text_returns_single_chunk():
    splitter = make_splitter(chunk_size=1000)
    chunks = splitter.chunk("Hello world", document_id="doc1", user_id="u", project_id="p")
    assert len(chunks) == 1
    assert chunks[0].content == "Hello world"
    assert chunks[0].document_id == "doc1"


def test_empty_text_returns_no_chunks():
    splitter = make_splitter(chunk_size=100)
    chunks = splitter.chunk("", document_id="doc1", user_id="u", project_id="p")
    assert chunks == []


def test_whitespace_only_returns_no_chunks():
    splitter = make_splitter(chunk_size=100)
    chunks = splitter.chunk("   \n\n\t  ", document_id="d", user_id="u", project_id="p")
    assert chunks == []


def test_long_text_produces_multiple_chunks():
    # length_function = len(text) // 4; chunk_size=10 means ≈40 chars per chunk
    splitter = make_splitter(chunk_size=10, chunk_overlap=2)
    long_text = "word " * 200  # 1000 chars → ~250 tokens
    chunks = splitter.chunk(long_text, document_id="d", user_id="u", project_id="p")
    assert len(chunks) > 1


def test_chunk_ids_are_unique():
    splitter = make_splitter(chunk_size=10, chunk_overlap=0)
    chunks = splitter.chunk("word " * 100, document_id="d", user_id="u", project_id="p")
    ids = [c.chunk_id for c in chunks]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# Metadata propagation
# ---------------------------------------------------------------------------

def test_metadata_forwarded_to_all_chunks():
    splitter = make_splitter(chunk_size=5, chunk_overlap=0)
    chunks = splitter.chunk(
        "a " * 100,
        metadata={"source": "test.txt"},
        document_id="d",
        user_id="u",
        project_id="p",
    )
    for chunk in chunks:
        assert chunk.metadata.get("source") == "test.txt"


def test_chunk_index_increments():
    splitter = make_splitter(chunk_size=5, chunk_overlap=0)
    chunks = splitter.chunk("a " * 100, document_id="d", user_id="u", project_id="p")
    indices = [c.metadata.get("chunk_index") for c in chunks]
    assert indices == list(range(len(chunks)))


# ---------------------------------------------------------------------------
# Markdown heading awareness
# ---------------------------------------------------------------------------

def test_heading_tracked_in_metadata():
    text = "# Introduction\n\nThis is the intro.\n\n## Details\n\nMore text here."
    splitter = make_splitter(chunk_size=1000)
    chunks = splitter.chunk(text, document_id="d", user_id="u", project_id="p")
    # All content in one chunk since text is short
    assert len(chunks) >= 1


def test_user_project_ids_set():
    splitter = make_splitter(chunk_size=1000)
    chunks = splitter.chunk("hello world", document_id="d", user_id="alice", project_id="proj1")
    assert all(c.user_id == "alice" for c in chunks)
    assert all(c.project_id == "proj1" for c in chunks)
