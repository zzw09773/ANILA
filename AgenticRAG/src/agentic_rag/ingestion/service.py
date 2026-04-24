"""Document ingestion service.

Orchestrates the full RAG ingest pipeline:

    parse → caption images → chunk (hierarchical) → embed leaves → index → persist

Features:
  - Idempotent: re-ingesting a document_id first deletes old chunks
  - Hierarchical chunking: parents stored for context, only leaves embedded
  - Vision: every ``[[IMAGE:id]]`` placeholder is resolved via an optional
    VisionProvider before chunking; images without a provider are kept as
    structural IMAGE leaves with placeholder captions
  - Batch embedding: leaves are sent to the embedding provider in batches
  - Progress callback: (current, total, stage)
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from ..models.storage import ChunkType, DocumentChunk
from .chunker import HierarchicalChunker
from .normalize import normalize_zh
from .parsers import ImageRef, ParsedDocument, ParserRegistry

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


@runtime_checkable
class _VisionProvider(Protocol):
    async def describe_image(
        self,
        image_bytes: bytes,
        mime: str = "image/png",
        prompt: Optional[str] = None,
        max_tokens: int = 512,
    ) -> str: ...


class IngestionService:
    """Ingest documents into the RAG pipeline.

    Args:
        embedding_provider: Provider implementing embed() and dimension.
        document_store:     Store for document chunks (metadata + content).
        retrieval_provider: Vector index store.
        vision_provider:    Optional VLM for captioning embedded images.
                            When None, image placeholders become
                            ``"[image]"`` captions but are still indexed
                            as IMAGE leaves under their heading.
        chunker:            Text splitter (default: HierarchicalChunker).
        embed_batch_size:   Number of chunks per embedding batch.
    """

    def __init__(
        self,
        embedding_provider: Any,
        document_store: Any,
        retrieval_provider: Any,
        vision_provider: Optional[Any] = None,
        chunker: Optional[HierarchicalChunker] = None,
        embed_batch_size: int = 50,
    ) -> None:
        self._embedder = embedding_provider
        self._doc_store = document_store
        self._retriever = retrieval_provider
        self._vision = vision_provider
        self._chunker = chunker or HierarchicalChunker()
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
        _progress(0, 5, "parsing")
        logger.info("Ingesting %s (doc_id=%s)", file_path, doc_id)
        parsed: ParsedDocument = ParserRegistry.parse(file_path)

        base_meta: dict = {
            "source_path": file_path,
            "document_id": doc_id,
            "format": parsed.format,
            **(metadata or {}),
            **parsed.metadata,
        }

        # Stage 2: Caption images (optional, depends on vision_provider)
        _progress(1, 5, "captioning_images")
        if parsed.images and self._vision is not None:
            await self._caption_images(parsed.images)
        elif parsed.images:
            logger.info(
                "doc_id=%s has %d images but no vision_provider configured — "
                "indexing as structural IMAGE leaves with placeholder captions",
                doc_id,
                len(parsed.images),
            )

        # Stage 3: Chunk
        _progress(2, 5, "chunking")
        normalized_text = normalize_zh(parsed.content)
        chunks = self._chunker.chunk(
            text=normalized_text,
            metadata=base_meta,
            document_id=doc_id,
            user_id=user_id,
            project_id=project_id,
            images=parsed.images,
        )
        logger.info("Produced %d chunks for doc_id=%s", len(chunks), doc_id)

        if not chunks:
            logger.warning("No chunks produced for %s — empty document?", file_path)
            return doc_id

        # Stage 4: Delete old data (idempotency) — scoped to same user/project
        _progress(3, 5, "cleanup_old")
        await self._doc_store.delete_document(
            doc_id, user_id=user_id, project_id=project_id
        )
        await self._retriever.delete_document(
            doc_id, user_id=user_id, project_id=project_id
        )

        # Stage 5: Embed leaves + Index all nodes (parents carry no embedding)
        _progress(4, 5, "embedding")
        leaves = [
            c for c in chunks
            if c.chunk_type in (ChunkType.CONTENT, ChunkType.IMAGE)
            and c.content.strip()
        ]
        leaf_texts = [c.content for c in leaves]
        leaf_embeddings = (
            await self._embedder.embed(leaf_texts, input_type="passage")
            if leaf_texts
            else []
        )
        embed_by_id = {
            leaf.chunk_id: emb for leaf, emb in zip(leaves, leaf_embeddings)
        }

        _progress(5, 5, "indexing")
        for chunk in chunks:
            embedded = chunk.model_copy(
                update={"embedding": embed_by_id.get(chunk.chunk_id)}
            )
            await self._doc_store.store(embedded)
            # Only leaves go into the vector index; parents are skipped
            # by the retrieval provider when their embedding is None.
            await self._retriever.index(embedded)

        logger.info(
            "Ingestion complete: doc_id=%s, chunks=%d (leaves=%d)",
            doc_id,
            len(chunks),
            len(leaves),
        )
        return doc_id

    async def delete(
        self,
        document_id: str,
        user_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> None:
        """Remove a document's chunks, scoped to user_id + project_id."""
        await self._doc_store.delete_document(
            document_id, user_id=user_id, project_id=project_id
        )
        await self._retriever.delete_document(
            document_id, user_id=user_id, project_id=project_id
        )
        logger.info(
            "Deleted doc_id=%s (user=%s project=%s)",
            document_id,
            user_id,
            project_id,
        )

    async def _caption_images(self, images: dict[str, ImageRef]) -> None:
        """Fill ``caption`` on every ImageRef in-place using the VLM."""
        for ref in images.values():
            try:
                ref.caption = await self._vision.describe_image(
                    ref.image_bytes, mime=ref.mime
                )
            except Exception as exc:
                logger.warning("VLM failed for image %s: %s", ref.image_id, exc)
                ref.caption = ""
