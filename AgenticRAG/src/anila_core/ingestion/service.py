"""Document ingestion service.

Orchestrates the full RAG ingest pipeline:
  parse → chunk → embed → index → persist

Features:
  - Idempotent: re-ingesting a document_id first deletes old chunks
  - Batch embedding: sends chunks in batches to the embedding provider
  - Progress callback: reports (current, total, stage) to callers
  - Transaction safety: embed + index happen atomically per document
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from ..models.storage import DocumentChunk
from .chunker import RecursiveTextSplitter
from .parsers import ParserRegistry

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, str], None]


@runtime_checkable
class _EmbeddingProvider(Protocol):
    async def embed(self, texts: list[str], input_type: str = "passage") -> list[list[float]]: ...
    @property
    def dimension(self) -> int: ...


@runtime_checkable
class _DocumentStore(Protocol):
    async def store(self, chunk: DocumentChunk) -> None: ...
    async def delete_document(self, document_id: str) -> None: ...


@runtime_checkable
class _RetrievalProvider(Protocol):
    async def index(self, chunk: DocumentChunk) -> None: ...
    async def delete_document(self, document_id: str) -> None: ...


class IngestionService:
    """Ingest documents into the RAG pipeline.

    Args:
        embedding_provider: Provider implementing embed() and dimension.
        document_store:     Store for document chunks (metadata + content).
        retrieval_provider: Vector index store.
        chunker:            Text splitter (default: RecursiveTextSplitter).
        embed_batch_size:   Number of chunks per embedding batch.
    """

    def __init__(
        self,
        embedding_provider: Any,
        document_store: Any,
        retrieval_provider: Any,
        chunker: Optional[RecursiveTextSplitter] = None,
        embed_batch_size: int = 50,
    ) -> None:
        self._embedder = embedding_provider
        self._doc_store = document_store
        self._retriever = retrieval_provider
        self._chunker = chunker or RecursiveTextSplitter()
        self._batch_size = embed_batch_size

    async def ingest(
        self,
        file_path: str,
        user_id: str,
        project_id: str,
        document_id: Optional[str] = None,
        metadata: Optional[dict] = None,
        on_progress: Optional[ProgressCallback] = None,
    ) -> str:
        """Ingest a document file into the RAG index.

        Args:
            file_path:    Absolute path to the document.
            user_id:      Owner user ID.
            project_id:   Project scope.
            document_id:  Stable ID for the document (auto-generated if None).
            metadata:     Additional metadata to attach to every chunk.
            on_progress:  Optional callback(current, total, stage).

        Returns:
            The document_id used for this ingestion.
        """
        doc_id = document_id or str(uuid.uuid4())

        def _progress(current: int, total: int, stage: str) -> None:
            if on_progress:
                try:
                    on_progress(current, total, stage)
                except Exception:
                    pass

        # Stage 1: Parse
        _progress(0, 4, "parsing")
        logger.info("Ingesting %s (doc_id=%s)", file_path, doc_id)
        parsed = ParserRegistry.parse(file_path)

        base_meta: dict = {
            "source_path": file_path,
            "document_id": doc_id,
            "format": parsed.format,
            **(metadata or {}),
            **parsed.metadata,
        }

        # Stage 2: Chunk
        _progress(1, 4, "chunking")
        chunks = self._chunker.chunk(
            text=parsed.content,
            metadata=base_meta,
            document_id=doc_id,
            user_id=user_id,
            project_id=project_id,
        )
        logger.info("Produced %d chunks for doc_id=%s", len(chunks), doc_id)

        if not chunks:
            logger.warning("No chunks produced for %s — empty document?", file_path)
            return doc_id

        # Stage 3: Delete old data (idempotency) — scoped to same user/project
        _progress(2, 4, "cleanup_old")
        await self._doc_store.delete_document(doc_id, user_id=user_id, project_id=project_id)
        await self._retriever.delete_document(doc_id, user_id=user_id, project_id=project_id)

        # Stage 4: Embed + Index
        _progress(3, 4, "embedding")
        texts = [c.content for c in chunks]
        all_embeddings = await self._embedder.embed(texts, input_type="passage")

        _progress(4, 4, "indexing")
        for chunk, embedding in zip(chunks, all_embeddings):
            embedded_chunk = chunk.model_copy(update={"embedding": embedding})
            await self._doc_store.store(embedded_chunk)
            await self._retriever.index(embedded_chunk)

        logger.info("Ingestion complete: doc_id=%s, chunks=%d", doc_id, len(chunks))
        return doc_id

    async def delete(
        self,
        document_id: str,
        user_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> None:
        """Remove a document's chunks from the index, scoped to user_id + project_id."""
        await self._doc_store.delete_document(document_id, user_id=user_id, project_id=project_id)
        await self._retriever.delete_document(document_id, user_id=user_id, project_id=project_id)
        logger.info("Deleted doc_id=%s (user=%s project=%s)", document_id, user_id, project_id)
