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
    API can return chunk metadata without shipping a 4096-d float
    array on every query — the inspector UI especially does NOT want
    to download tens of MB of vectors per page render.
    """

    id: int
    collection_id: int
    agent_id: int
    document_id: int
    chunk_key: str
    content: str
    embedding: Optional[list[float]] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    token_count: Optional[int] = None
    created_at: datetime


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
