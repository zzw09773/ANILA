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
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from app.schemas.base import ApiResponseModel
from app.schemas.contracts.classification import ClassificationLevel

# Product surfaces that may own a collection shelf. Mirrors the attic
# provenance vocabulary and conversations' ``anilalm`` tag. NULL on
# the row means pre-origin legacy (see migration r1_0029).
CollectionOrigin = Literal["csp", "anilalm"]


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
    """Payload to ``POST /api/ingestion/collections``.

    Sprint 4: ``agent_id`` is gone. ``created_by`` is auto-set to the
    authenticated user; collections are user-owned and reusable across
    any agent backend that points at them.

    ``classification_level`` defaults to 無機密 — one select on the form,
    not a workflow. ``origin`` tags the creating product surface
    (``csp`` / ``anilalm``); omit only for legacy callers that accept
    dual-surface visibility.
    """

    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    chunking_config: ChunkingConfig
    embedding_model: str | None = Field(
        default=None,
        description=(
            "Embedding model name. When omitted, defaults to the platform's "
            "designated embedding model (is_platform_embedding). Existing "
            "collections keep their stored value and cannot change it."
        ),
    )
    embedding_dim: int = Field(
        default=4000,
        ge=64,
        le=4000,
        description=(
            "Vector dimension used by ``document_chunks.embedding``. Must match "
            "the live schema column — currently halfvec(4000) per migration "
            "0015. Native widths above 4000 are truncated by the worker."
        ),
    )
    classification_level: str = Field(
        default="無機密",
        description="四級分類：無機密 < 營業秘密 < 密 < 機密。預設無機密。",
    )
    origin: Optional[CollectionOrigin] = Field(
        default=None,
        description=(
            "Creating product surface: 'csp' (governance) or 'anilalm'. "
            "NULL = legacy dual-surface visibility."
        ),
    )

    @field_validator("classification_level")
    @classmethod
    def _validate_classification_level(cls, value: str) -> str:
        try:
            return ClassificationLevel.from_storage(value).to_storage()
        except ValueError as exc:
            raise ValueError(
                "classification_level 必須是四級之一：無機密、營業秘密、密、機密"
            ) from exc


class CollectionUpdate(BaseModel):
    """Payload to ``PATCH /api/ingestion/collections/{id}``.

    All fields optional — only provided ones are updated. Re-keying
    ``embedding_model`` or ``embedding_dim`` after data is indexed is
    intentionally NOT allowed by the API (would silently invalidate
    every existing embedding); both fields are absent here. Reindex
    happens via a future ``POST /reindex`` endpoint.

    ``classification_level`` is intentionally absent — raising a
    collection's level is a dedicated route
    (``POST .../classification``) so ordinary updates cannot silently
    latch or bypass the stranded-bind / document-cascade checks.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    chunking_config: ChunkingConfig | None = None
    status: str | None = Field(
        default=None,
        pattern="^(active|archived)$",
        description="'active' or 'archived'. Use DELETE to actually drop.",
    )


class CollectionClassificationRaise(BaseModel):
    """Payload to ``POST /api/ingestion/collections/{id}/classification``.

    Raise-only. Lowering must go through the declassification request
    flow; the route refuses a non-raise rather than letting
    ``apply_classification`` no-op into a misleading 200.
    """

    classification_level: str = Field(
        ...,
        description="目標密等（須嚴格高於現行）；無機密 < 營業秘密 < 密 < 機密",
    )

    @field_validator("classification_level")
    @classmethod
    def _validate_classification_level(cls, value: str) -> str:
        try:
            return ClassificationLevel.from_storage(value).to_storage()
        except ValueError as exc:
            raise ValueError(
                "classification_level 必須是四級之一：無機密、營業秘密、密、機密"
            ) from exc


# ── Collection: response shapes ─────────────────────────────────────────────


class CollectionResponse(ApiResponseModel):
    """Full row projection used by both list and detail endpoints.

    Sprint 4: ``agent_id`` removed; ``created_by`` is the new ownership
    field (always populated post-migration 0019).
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    chunking_config: dict[str, Any]
    embedding_model: str
    embedding_dim: int
    status: str
    document_count: int
    chunk_count: int
    bytes_stored: int
    created_by: int
    origin: str | None = None
    classification_level: str = "無機密"
    created_at: datetime
    updated_at: datetime


# ── Document relations (cross-document edges, design v2 §3/§8) ───────────────

# The 6 edge types the citation extractor classifies (design v2 §5). ``relates``
# is the catch-all when a regulation name is cited with no directional cue.
RelationType = Literal[
    "based_on", "amends", "supersedes", "cites", "supplements", "relates"
]
# Who authored the edge. ``rule`` = regex citation extractor, ``manual`` = a
# human via the API, ``llm`` = LLM extractor (Phase 2), ``similarity`` =
# embedding topic-similarity (C track). All coexist in one table; the UNIQUE
# key includes ``source`` (codex #9).
RelationSource = Literal["rule", "manual", "llm", "similarity"]


class DocumentRelationCreate(BaseModel):
    """Payload to ``POST /api/ingestion/collections/{id}/relations`` — a manual
    edge (``source='manual'``).

    The edge always starts at ``src_document_id`` (an existing document in the
    collection) and points at either a concrete ``dst_document_id`` OR an
    unresolved ``target_ref`` string (a regulation name not yet uploaded).
    At least one of the two must be supplied; if both are, ``dst_document_id``
    wins and ``target_ref`` is recorded for provenance.
    """

    src_document_id: int = Field(..., description="Edge origin; must be in this collection.")
    relation_type: RelationType
    dst_document_id: int | None = Field(
        default=None, description="Resolved target document in the same collection."
    )
    target_ref: str | None = Field(
        default=None,
        max_length=500,
        description="Unresolved target name [+article]; back-filled to dst later.",
    )
    evidence: str | None = Field(
        default=None, max_length=2000, description="Optional human note / cited sentence."
    )

    @model_validator(mode="after")
    def _require_a_target(self) -> "DocumentRelationCreate":
        if self.dst_document_id is None and not (self.target_ref and self.target_ref.strip()):
            raise ValueError("one of dst_document_id or target_ref is required")
        return self


class DocumentRelationResponse(ApiResponseModel):
    """Row projection for the relations tab / API list.

    Carries enough to render ``src → type → dst|target_ref`` with the
    unresolved (``dst_document_id is None``) and ``ambiguous`` cases flagged.
    ``src_title`` / ``dst_title`` / ``ambiguous`` are enriched by the API layer
    (joins + a target-collision check), not stored columns — defaults keep this
    schema usable straight from a bare ORM row.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    collection_id: int
    src_document_id: int
    dst_document_id: int | None
    dst_chunk_id: int | None
    target_ref: str
    relation_type: RelationType
    confidence: float
    source: RelationSource
    extractor_run_id: str | None
    evidence: str | None
    created_at: datetime | None
    created_by_user_id: int | None

    # API-enriched, non-persisted niceties (default so ORM validation works).
    src_title: str | None = None
    dst_title: str | None = None
    resolved: bool = False
    ambiguous: bool = False


class ReresolveResponse(BaseModel):
    """Result of ``POST .../relations:reresolve`` — counts after a re-extract
    + reconciliation pass over the collection (design v2 §7)."""

    rule_edges_extracted: int = 0
    resolved: int = 0
    unresolved: int = 0
    ambiguous: int = 0
