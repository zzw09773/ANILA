"""Ingestion / retrieval Pydantic models.

These were imported from ``anila_core.models.ingestion`` until Phase 0
decoupling (2026-05-02). AgenticRAG now owns its own copy so devs can
fork the template without pulling in the platform-internal package.

The Phase 0 plan (docs/agenticrag-decouple-from-anila-core.md) freezes
the wire shape of these types as part of the AgenticRAG ⟷ ingestion
contract. Platform deployments that inject anila-core's
``CollectionScopedPgVectorStore`` continue to work because both types
have identical field layouts.

Naming: ``IngestionChunk`` is intentionally distinct from the legacy
``DocumentChunk`` in ``models/storage.py`` (which uses TEXT chunk_id /
user_id / project_id keys, predates the ingestion platform). Don't mix
them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class IngestionChunk(BaseModel):
    """A persisted chunk in the pgvector store.

    Mirrors a ``document_chunks`` row. The ``embedding`` field is
    optional in the model so list endpoints can return chunk metadata
    without shipping a 1536-d vector per row to the inspector UI.

    Sprint 9 X / parent-child fields (``parent_chunk_id`` / ``chunk_type``
    / ``chunk_level``) default to NULL / "leaf" / 0 for legacy rows and
    flat-chunk strategies. Hierarchical chunkers populate them.
    """

    id: int
    collection_id: int
    document_id: int
    chunk_key: str
    content: str
    embedding: Optional[list[float]] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    token_count: Optional[int] = None
    created_at: datetime
    parent_chunk_id: Optional[int] = None
    chunk_type: str = "leaf"
    chunk_level: int = 0


class SearchHit(BaseModel):
    """Result of a similarity / keyword search.

    Distinct from ``IngestionChunk`` because hits carry a per-call
    ``score`` (computation-derived, not persisted) and may have an
    attached ``parent_content`` resolved at retrieval time when the
    matched leaf has a hierarchical parent.

    ``score`` semantics depend on the search:
      - similarity_search: cosine similarity in [0, 1] (higher closer)
      - keyword_search: ``ts_rank_cd`` raw value (compare ranks, not axes)

    Mixing the two requires merging by rank position (RRF) rather than
    by score axis.
    """

    chunk: IngestionChunk
    score: float
    parent_content: Optional[str] = None
