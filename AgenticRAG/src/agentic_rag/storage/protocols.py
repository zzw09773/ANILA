"""VectorStore Protocol — DI surface for plugging alternative impls.

Phase 0 decoupling (2026-05-02): AgenticRAG depends on this Protocol,
not on a specific class. The default impl is
``CollectionScopedPgVectorStore`` in ``agentic_rag.storage.adapters``;
platform deployments can inject anila-core's RLS-aware variant via
the ``vector_store_override`` hook in ``app_factory.build_app``.

Protocol over ABC because:
  - Duck typing is friendlier to forks. Devs swapping in Qdrant /
    Weaviate / Pinecone don't need to subclass — they just need to
    ship the same shape.
  - ``runtime_checkable`` lets us assert the contract at the override
    boundary without forcing full nominal compatibility.

The signatures must stay identical to ``CollectionScopedPgVectorStore``
so any conforming impl is a drop-in. New methods added to the default
impl that aren't in this Protocol are fine — they just won't be visible
to code that types against the Protocol.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..ingestion.chunking_types import ChunkResult
from ..models.ingestion import IngestionChunk, SearchHit


@runtime_checkable
class VectorStore(Protocol):
    """Minimum surface every vector-store implementation must satisfy.

    ``collection_id`` semantics: implementations MUST scope all
    operations to a single collection. The expected pattern is
    construction-time scoping (one instance per collection per request)
    so RLS / namespace enforcement happens in one place.
    """

    @property
    def collection_id(self) -> int: ...

    # ── Write path ──────────────────────────────────────────────────────────

    async def index_chunks(
        self,
        document_id: int,
        chunks: list[ChunkResult],
        embeddings: list[list[float]],
        parent_id_map: dict[str, int] | None = None,
    ) -> int:
        """Bulk-insert leaf chunks with embeddings. Returns rows written."""
        ...

    async def add_parent_chunks(
        self,
        document_id: int,
        chunks: list,
    ) -> dict[str, int]:
        """Insert non-leaf parent rows. Returns ``{chunk_key: db_id}``."""
        ...

    # ── Read path ───────────────────────────────────────────────────────────

    async def similarity_search(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        min_score: float = 0.0,
    ) -> list[SearchHit]:
        """Vector cosine-similarity search. Score in [0, 1] (higher closer)."""
        ...

    async def keyword_search(
        self,
        query: str,
        top_k: int = 10,
        tokenized_query: str | None = None,
    ) -> list[SearchHit]:
        """Full-text keyword search. Score is impl-defined (rank with RRF)."""
        ...

    async def list_by_document(
        self,
        document_id: int,
        limit: int = 100,
        offset: int = 0,
        include_embedding: bool = False,
    ) -> list[IngestionChunk]:
        """Paginated chunks for one document."""
        ...

    async def list_in_collection(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> list[IngestionChunk]:
        """Paginated chunks across the whole scoped collection."""
        ...

    # ── Delete path ─────────────────────────────────────────────────────────

    async def delete_document(self, document_id: int) -> int:
        """Delete every chunk for one document. Returns count deleted."""
        ...

    async def delete_all(self) -> int:
        """Delete every chunk in this collection. Returns count deleted."""
        ...
