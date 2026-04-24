"""Tests for IngestionService with mock embedding and mock stores."""

from __future__ import annotations

import pytest
from pathlib import Path

from agentic_rag.ingestion.service import IngestionService
from agentic_rag.ingestion.chunker import HierarchicalChunker
from agentic_rag.models.storage import ChunkType, DocumentChunk
from agentic_rag.providers.embedding_mock import MockEmbeddingProvider
from agentic_rag.providers.vision import MockVisionProvider


# ---------------------------------------------------------------------------
# Mock stores
# ---------------------------------------------------------------------------

class InMemoryDocStore:
    def __init__(self):
        self._chunks: dict[str, DocumentChunk] = {}

    async def store(self, chunk: DocumentChunk) -> None:
        self._chunks[chunk.chunk_id] = chunk

    async def retrieve(self, chunk_id: str):
        return self._chunks.get(chunk_id)

    async def list_by_document(self, document_id: str) -> list[DocumentChunk]:
        return [c for c in self._chunks.values() if c.document_id == document_id]

    async def delete_document(self, document_id: str, **_kwargs) -> None:
        to_delete = [k for k, c in self._chunks.items() if c.document_id == document_id]
        for k in to_delete:
            del self._chunks[k]


class InMemoryRetriever:
    def __init__(self, doc_store: InMemoryDocStore):
        self._store = doc_store
        self.indexed: list[str] = []

    async def index(self, chunk: DocumentChunk) -> None:
        self.indexed.append(chunk.chunk_id)

    async def delete_document(self, document_id: str, **_kwargs) -> None:
        pass  # nothing to do in this mock


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def doc_store():
    return InMemoryDocStore()

@pytest.fixture
def retriever(doc_store):
    return InMemoryRetriever(doc_store)

@pytest.fixture
def service(doc_store, retriever):
    return IngestionService(
        embedding_provider=MockEmbeddingProvider(dimension=64),
        document_store=doc_store,
        retrieval_provider=retriever,
        vision_provider=MockVisionProvider("mocked caption"),
        chunker=HierarchicalChunker(max_leaf_tokens=20, overlap_tokens=5),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_txt_file(service, doc_store, tmp_path: Path):
    f = tmp_path / "sample.txt"
    f.write_text("Hello world. " * 50)

    doc_id = await service.ingest(
        file_path=str(f),
        user_id="alice",
        project_id="proj",
    )

    assert doc_id  # has a value
    chunks = await doc_store.list_by_document(doc_id)
    assert len(chunks) > 0

    # Only leaves (content / image) carry an embedding; parents (document,
    # heading) intentionally do not.
    leaves = [c for c in chunks if c.chunk_type in (ChunkType.CONTENT, ChunkType.IMAGE)]
    parents = [c for c in chunks if c.chunk_type in (ChunkType.DOCUMENT, ChunkType.HEADING)]
    assert leaves, "expected at least one leaf chunk"
    assert all(c.embedding is not None and len(c.embedding) == 64 for c in leaves)
    assert all(c.embedding is None for c in parents)


@pytest.mark.asyncio
async def test_ingest_md_file(service, doc_store, tmp_path: Path):
    f = tmp_path / "guide.md"
    f.write_text("# Title\n\n" + "Content paragraph. " * 30)

    doc_id = await service.ingest(str(f), user_id="u", project_id="p")
    chunks = await doc_store.list_by_document(doc_id)
    assert len(chunks) > 0


@pytest.mark.asyncio
async def test_ingest_is_idempotent(service, doc_store, tmp_path: Path):
    f = tmp_path / "doc.txt"
    f.write_text("Some content. " * 20)

    doc_id = "fixed-id"
    await service.ingest(str(f), user_id="u", project_id="p", document_id=doc_id)
    count_first = len(await doc_store.list_by_document(doc_id))

    # Re-ingest same document
    await service.ingest(str(f), user_id="u", project_id="p", document_id=doc_id)
    count_second = len(await doc_store.list_by_document(doc_id))

    assert count_first == count_second  # idempotent: same chunk count


@pytest.mark.asyncio
async def test_ingest_calls_progress(service, tmp_path: Path):
    f = tmp_path / "doc.txt"
    f.write_text("Hello. " * 10)

    stages: list[str] = []

    def on_progress(current, total, stage):
        stages.append(stage)

    await service.ingest(str(f), user_id="u", project_id="p", on_progress=on_progress)
    assert "parsing" in stages
    assert "chunking" in stages
    assert "indexing" in stages


@pytest.mark.asyncio
async def test_delete_removes_chunks(service, doc_store, tmp_path: Path):
    f = tmp_path / "doc.txt"
    f.write_text("Content. " * 20)

    doc_id = "del-test"
    await service.ingest(str(f), user_id="u", project_id="p", document_id=doc_id)
    assert len(await doc_store.list_by_document(doc_id)) > 0

    await service.delete(doc_id)
    assert len(await doc_store.list_by_document(doc_id)) == 0
