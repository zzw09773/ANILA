"""Tests for the Citation model and the vision ingestion path."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_rag.ingestion.chunker import HierarchicalChunker
from agentic_rag.ingestion.parsers import ImageRef
from agentic_rag.ingestion.service import IngestionService
from agentic_rag.models.storage import ChunkType, Citation
from agentic_rag.providers.embedding_mock import MockEmbeddingProvider
from agentic_rag.providers.vision import MockVisionProvider
from agentic_rag.models.storage import DocumentChunk


# ──────────────────────────────────────────────────────────────────────
# Citation.cite()
# ──────────────────────────────────────────────────────────────────────

def test_citation_cite_full_trail():
    c = Citation(
        chunk_id="c1",
        document_id="d1",
        document_title="Army Law",
        heading_path=["Chapter 1", "Article 8"],
        page=3,
        content="body",
        confidence=0.87,
    )
    label = c.cite()
    assert "Army Law" in label
    assert "Chapter 1" in label
    assert "Article 8" in label
    assert "p.3" in label


def test_citation_cite_page_only():
    c = Citation(
        chunk_id="c1",
        document_id="d1",
        page=5,
        content="body",
    )
    assert c.cite() == "p.5"


def test_citation_cite_fallback_to_document_id():
    c = Citation(chunk_id="c1", document_id="doc-xyz", content="body")
    assert c.cite() == "doc-xyz"


# ──────────────────────────────────────────────────────────────────────
# Ingestion with image placeholders gets captioned
# ──────────────────────────────────────────────────────────────────────

class _InMemStore:
    def __init__(self) -> None:
        self.chunks: dict[str, DocumentChunk] = {}

    async def store(self, chunk: DocumentChunk) -> None:
        self.chunks[chunk.chunk_id] = chunk

    async def list_by_document(self, document_id: str, **_kw) -> list[DocumentChunk]:
        return [c for c in self.chunks.values() if c.document_id == document_id]

    async def delete_document(self, document_id: str, **_kw) -> None:
        to_del = [k for k, c in self.chunks.items() if c.document_id == document_id]
        for k in to_del:
            del self.chunks[k]


class _NoopRetriever:
    async def index(self, _chunk: DocumentChunk) -> None:
        pass

    async def delete_document(self, _document_id: str, **_kw) -> None:
        pass


@pytest.mark.asyncio
async def test_vision_captions_embedded_images_in_hierarchical_chunks(tmp_path: Path):
    """A markdown file with an [[IMAGE:...]] marker ends up as a captioned
    IMAGE leaf embedded under the surrounding heading."""
    # Craft a parsed document by going through the chunker directly —
    # this keeps the test free of the PDF / DOCX deps.
    text = (
        "# Report\n\n"
        "The first finding is shown in the figure below.\n\n"
        "[[IMAGE:img_1]]\n\n"
        "Conclusion text.\n"
    )
    images = {
        "img_1": ImageRef(
            image_id="img_1",
            image_bytes=b"\x89PNGbytes",
            mime="image/png",
            caption="",  # empty — VLM hasn't run yet
        )
    }

    # Simulate the VLM step
    vision = MockVisionProvider("a bar chart comparing Q1 and Q2 revenue")
    for ref in images.values():
        ref.caption = await vision.describe_image(ref.image_bytes, mime=ref.mime)

    chunker = HierarchicalChunker()
    chunks = chunker.chunk(
        text,
        document_id="d1",
        user_id="u",
        project_id="p",
        images=images,
        metadata={"title": "Report"},
    )

    image_leaves = [c for c in chunks if c.chunk_type == ChunkType.IMAGE]
    assert len(image_leaves) == 1
    assert "bar chart" in image_leaves[0].content
    assert image_leaves[0].heading_path == ["Report"]
    assert image_leaves[0].metadata["image_id"] == "img_1"


@pytest.mark.asyncio
async def test_ingestion_service_stores_hierarchy(tmp_path: Path):
    doc_store = _InMemStore()
    service = IngestionService(
        embedding_provider=MockEmbeddingProvider(dimension=32),
        document_store=doc_store,
        retrieval_provider=_NoopRetriever(),
        vision_provider=MockVisionProvider(),
        chunker=HierarchicalChunker(),
    )

    md = tmp_path / "doc.md"
    md.write_text("# Top\n\npara a\n\n## Sub\n\npara b\n")

    doc_id = await service.ingest(str(md), user_id="u", project_id="p")
    stored = await doc_store.list_by_document(doc_id)

    # Expect: 1 document root + 2 headings + 2 content leaves = 5 nodes
    types = [c.chunk_type for c in stored]
    assert types.count(ChunkType.DOCUMENT) == 1
    assert types.count(ChunkType.HEADING) == 2
    assert types.count(ChunkType.CONTENT) >= 2

    # Parent links must form a tree
    by_id = {c.chunk_id: c for c in stored}
    for c in stored:
        if c.parent_chunk_id:
            assert c.parent_chunk_id in by_id
            assert by_id[c.parent_chunk_id].chunk_level < c.chunk_level
