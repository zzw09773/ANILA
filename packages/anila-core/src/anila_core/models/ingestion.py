"""Ingestion / retrieval Pydantic models (v0.6.0 Phase 2 Sprint 1).

The post-cleanup ``models.storage.DocumentChunk`` predates the ingestion
platform and uses the legacy ``(chunk_id TEXT, user_id, project_id)``
shape. The new schema is ``(id BIGINT, agent_id, collection_id,
document_id BIGINT FK, chunk_key TEXT)`` — same conceptual entity but
incompatible field names and types.

Rather than mutate the old model in place (which would break every
existing memory / dispatch consumer), we introduce a new ``IngestionChunk``
model. The old type stays where it is; new ingestion code uses these
types exclusively.

Naming: ``IngestionChunk`` over ``DocumentChunkV2`` because the only
overlap with the legacy class is the conceptual one (a slice of a
document) — the surrounding metadata, scope semantics and storage shape
are entirely different. A "v2" name would mislead callers into thinking
this is a drop-in replacement.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class IngestionChunk(BaseModel):
    """A persisted chunk in the ingestion platform's pgvector table.

    Mirrors the ``document_chunks`` row introduced by migration 0014.
    The ``embedding`` field is optional in this Pydantic model so the
    API can return chunk metadata without shipping a 1536-d float
    array on every query — the inspector UI especially does NOT want
    to download MB of vectors per page render. Inspector loads the
    vector only when the dev opens the "show vector debug" panel.
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
    # Sprint 9 X / parent-child RAG (migration 0028). All NULL for rows
    # written before the migration / new chunker landed; populated
    # going forward by HierarchicalChunker.
    parent_chunk_id: Optional[int] = None
    chunk_type: str = "leaf"
    chunk_level: int = 0


class SearchHit(BaseModel):
    """Result of a similarity search.

    Distinct from ``IngestionChunk`` because hits carry the score (which
    is computation-derived, not persisted) and may omit fields the
    caller didn't ask for. Keeping these as separate types makes it
    easier to reason about which fields are guaranteed populated.
    """

    chunk: IngestionChunk
    # Cosine *similarity* in [0, 1] — higher is closer. We convert
    # asyncpg's cosine *distance* output here so the rest of the platform
    # can speak in the more intuitive direction without per-call flipping.
    score: float
    # Sprint 9 X — parent's content, JOIN-fetched at retrieval time
    # when the matched leaf has a ``parent_chunk_id``. None for legacy
    # rows or root-level chunks.
    parent_content: Optional[str] = None
