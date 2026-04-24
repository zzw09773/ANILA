"""Tests for HierarchicalChunker (and the RecursiveTextSplitter alias)."""

from __future__ import annotations

from agentic_rag.ingestion.chunker import HierarchicalChunker, RecursiveTextSplitter
from agentic_rag.ingestion.parsers import ImageRef
from agentic_rag.models.storage import ChunkType


def _leaves(chunks):
    return [c for c in chunks if c.chunk_type in (ChunkType.CONTENT, ChunkType.IMAGE)]


def _headings(chunks):
    return [c for c in chunks if c.chunk_type == ChunkType.HEADING]


def _roots(chunks):
    return [c for c in chunks if c.chunk_type == ChunkType.DOCUMENT]


# ---------------------------------------------------------------------------
# Basic shape
# ---------------------------------------------------------------------------

def test_empty_text_still_produces_document_root():
    chunker = HierarchicalChunker()
    chunks = chunker.chunk("", document_id="d", user_id="u", project_id="p")
    # Even an empty file has a document root so citations have a stable parent.
    assert len(chunks) == 1
    assert chunks[0].chunk_type == ChunkType.DOCUMENT
    assert chunks[0].parent_chunk_id is None


def test_plain_text_creates_root_plus_content_leaf():
    chunker = HierarchicalChunker()
    chunks = chunker.chunk(
        "Hello world", document_id="doc1", user_id="u", project_id="p"
    )
    assert len(_roots(chunks)) == 1
    leaves = _leaves(chunks)
    assert len(leaves) == 1
    assert leaves[0].content == "Hello world"
    assert leaves[0].parent_chunk_id == _roots(chunks)[0].chunk_id
    assert leaves[0].chunk_level == 1


def test_chunk_ids_are_unique():
    chunker = HierarchicalChunker()
    chunks = chunker.chunk(
        "# H1\n\npara a\n\npara b\n\n## H2\n\npara c",
        document_id="d",
        user_id="u",
        project_id="p",
    )
    ids = [c.chunk_id for c in chunks]
    assert len(ids) == len(set(ids))


def test_user_project_ids_propagate():
    chunker = HierarchicalChunker()
    chunks = chunker.chunk(
        "hello", document_id="d", user_id="alice", project_id="proj1"
    )
    assert all(c.user_id == "alice" for c in chunks)
    assert all(c.project_id == "proj1" for c in chunks)


# ---------------------------------------------------------------------------
# Heading hierarchy
# ---------------------------------------------------------------------------

def test_heading_path_is_built_from_structure():
    text = (
        "# Chapter 1\n\n"
        "intro text\n\n"
        "## Section 1.1\n\n"
        "detail text\n\n"
        "## Section 1.2\n\n"
        "more detail\n\n"
        "# Chapter 2\n\n"
        "other text\n"
    )
    chunker = HierarchicalChunker()
    chunks = chunker.chunk(text, document_id="d", user_id="u", project_id="p")

    leaves = _leaves(chunks)
    paths = [tuple(leaf.heading_path) for leaf in leaves]
    assert ("Chapter 1",) in paths
    assert ("Chapter 1", "Section 1.1") in paths
    assert ("Chapter 1", "Section 1.2") in paths
    assert ("Chapter 2",) in paths


def test_parent_links_form_a_tree():
    text = "# A\n\ntext\n\n## B\n\nmore text"
    chunker = HierarchicalChunker()
    chunks = chunker.chunk(text, document_id="d", user_id="u", project_id="p")

    by_id = {c.chunk_id: c for c in chunks}
    for c in chunks:
        if c.parent_chunk_id is not None:
            assert c.parent_chunk_id in by_id, "parent must exist"
            assert by_id[c.parent_chunk_id].chunk_level < c.chunk_level


def test_heading_becomes_heading_node():
    chunker = HierarchicalChunker()
    chunks = chunker.chunk(
        "# Alpha\n\nbody text",
        document_id="d",
        user_id="u",
        project_id="p",
    )
    heading_nodes = _headings(chunks)
    assert len(heading_nodes) == 1
    assert heading_nodes[0].content == "Alpha"
    assert heading_nodes[0].chunk_level == 1


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

def test_image_marker_becomes_image_leaf_with_caption():
    text = "# Fig\n\nSee the chart below.\n\n[[IMAGE:img_abc]]\n\nEnd."
    images = {
        "img_abc": ImageRef(
            image_id="img_abc",
            image_bytes=b"PNGDATA",
            mime="image/png",
            caption="A bar chart showing quarterly revenue.",
        ),
    }
    chunker = HierarchicalChunker()
    chunks = chunker.chunk(
        text,
        document_id="d",
        user_id="u",
        project_id="p",
        images=images,
    )

    image_leaves = [c for c in chunks if c.chunk_type == ChunkType.IMAGE]
    assert len(image_leaves) == 1
    img = image_leaves[0]
    assert "bar chart" in img.content
    assert img.heading_path == ["Fig"]
    assert img.metadata["image_id"] == "img_abc"
    assert img.metadata["mime"] == "image/png"


def test_image_without_caption_still_indexed():
    text = "# Hdr\n\n[[IMAGE:img_nope]]"
    chunker = HierarchicalChunker()
    chunks = chunker.chunk(
        text, document_id="d", user_id="u", project_id="p", images={}
    )
    image_leaves = [c for c in chunks if c.chunk_type == ChunkType.IMAGE]
    assert len(image_leaves) == 1
    assert image_leaves[0].content == "[image]"


# ---------------------------------------------------------------------------
# Size fallback
# ---------------------------------------------------------------------------

def test_oversized_paragraph_is_subsplit():
    # max_leaf_tokens=5 → 1 token ~= 4 chars, so ~20 chars cap per leaf
    chunker = HierarchicalChunker(max_leaf_tokens=5, overlap_tokens=0)
    body = "abcdefg " * 40  # ~320 chars
    chunks = chunker.chunk(body, document_id="d", user_id="u", project_id="p")
    leaves = _leaves(chunks)
    assert len(leaves) >= 3


# ---------------------------------------------------------------------------
# Back-compat alias
# ---------------------------------------------------------------------------

def test_recursive_alias_still_works():
    splitter = RecursiveTextSplitter(chunk_size=1024, chunk_overlap=64)
    chunks = splitter.chunk("hello", document_id="d", user_id="u", project_id="p")
    leaves = _leaves(chunks)
    assert leaves and leaves[0].content == "hello"
