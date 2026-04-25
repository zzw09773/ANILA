"""Pydantic schemas for the ingestion platform API.

Request shapes are deliberately restrictive — each endpoint accepts the
narrowest possible payload to keep the API surface auditable. Response
shapes are the projection of the full ORM row that the dev UI actually
needs (no internals leak; no embedding payload by default).

Sprint 1 covers collections only. Document upload and job tracking move
to Sprint 2 alongside the worker.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ── Chunking config ─────────────────────────────────────────────────────────


class ChunkingConfig(BaseModel):
    """Validated chunking spec stored on the collection.

    The full param schema for each strategy lives in
    ``anila_core.ingestion.chunking_plugins`` (registry returns a JSON
    schema per built-in). The API accepts strategy + free-form params
    here; the worker validates against the strategy's schema at
    ingestion time, so a typo in ``chunking_config`` doesn't fail until
    the first upload — by design (lets a dev experiment with custom
    plug-ins without re-validating the API).
    """

    strategy: str = Field(
        ...,
        description="Chunking strategy name (e.g. 'hierarchical' / 'fixed' / 'markdown-aware')",
        examples=["hierarchical"],
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Strategy-specific params; merged with strategy defaults at chunk time.",
    )


# ── Collection: request shapes ──────────────────────────────────────────────


class CollectionCreate(BaseModel):
    """Payload to ``POST /api/ingestion/collections``."""

    agent_id: int = Field(..., description="Owning agent. Must be agent the caller has access to.")
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    chunking_config: ChunkingConfig
    embedding_model: str = Field(
        default="nvidia/NV-embed-V2",
        description="Embedding endpoint identifier; worker resolves to a credential.",
    )
    embedding_dim: int = Field(
        default=1536,
        ge=64,
        le=4000,
        description=(
            "Vector dimension used by ``document_chunks.embedding``. Must match "
            "the live ``vector(N)`` schema column — currently 1536."
        ),
    )


class CollectionUpdate(BaseModel):
    """Payload to ``PATCH /api/ingestion/collections/{id}``.

    All fields optional — only provided ones are updated. Re-keying
    ``embedding_model`` or ``embedding_dim`` after data is indexed is
    intentionally NOT allowed by the API (would silently invalidate
    every existing embedding); both fields are absent here. Reindex
    happens via a future ``POST /reindex`` endpoint.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    chunking_config: ChunkingConfig | None = None
    status: str | None = Field(
        default=None,
        pattern="^(active|archived)$",
        description="'active' or 'archived'. Use DELETE to actually drop.",
    )


# ── Collection: response shapes ─────────────────────────────────────────────


class CollectionResponse(BaseModel):
    """Full row projection used by both list and detail endpoints."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    agent_id: int
    name: str
    description: str | None
    chunking_config: dict[str, Any]
    embedding_model: str
    embedding_dim: int
    status: str
    document_count: int
    chunk_count: int
    bytes_stored: int
    created_by: int | None
    created_at: datetime
    updated_at: datetime
