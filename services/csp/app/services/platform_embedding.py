"""Platform embedding model resolution (P4.8).

One designated ``model_registry`` row (``is_platform_embedding``) is the
single source of truth for:

* memory embed / retrieve
* new knowledge-collection defaults
* ingestion-worker embed requests

Mirrors the ``is_router_primary`` shape. Callers never match embedding
models by a hardcoded name string — that was the silent production
defect (``nvidia/NV-embed-V2`` vs registered ``nvidia/nv-embed-v2``).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from anila_core.memory.long_term import EMBED_DIM

from app.models.model_registry import ModelRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlatformEmbedding:
    """Resolved designation (or fallback) for embed callers."""

    model: ModelRegistry
    native_dim: int
    truncates: bool

    @property
    def name(self) -> str:
        return self.model.name

    @property
    def pad_from(self) -> Optional[int]:
        """Declared source dim for ``truncate_embedding``; None when
        native equals the column width (passthrough) or the legacy
        4096 path can rely on ``EMBED_NATIVE_DIM``."""
        if self.native_dim == EMBED_DIM:
            return None
        return self.native_dim


def _native_dim_of(row: ModelRegistry) -> int:
    measured = getattr(row, "embedding_native_dim", None)
    if isinstance(measured, int) and measured > 0:
        return measured
    # Pre-designation / backfilled rows without a probe: treat column
    # width as native so truncate is a no-op rather than inventing a
    # dimension. Callers that need a real probe go through set-platform-
    # embedding, which always measures.
    return EMBED_DIM


def resolve_platform_embedding(db: Session) -> Optional[PlatformEmbedding]:
    """Return the designated platform embedding, or a soft fallback.

    Preference order:
      1. ``is_platform_embedding=true`` (and active)
      2. first active ``model_type='embedding'`` row (id asc)

    Fallback exists so memory / worker keep working before an admin
    clicks "設為平台主 embedding" — the owner's standing instruction is
    not to introduce a new gate. Logging makes the missing designation
    visible without failing the turn.
    """
    designated: ModelRegistry | None = (
        db.query(ModelRegistry)
        .filter(
            ModelRegistry.is_platform_embedding.is_(True),
            ModelRegistry.model_type == "embedding",
        )
        .first()
    )
    if designated is not None:
        if not designated.is_active:
            logger.warning(
                "platform embedding %r is designated but inactive",
                designated.name,
            )
            return None
        native = _native_dim_of(designated)
        return PlatformEmbedding(
            model=designated,
            native_dim=native,
            truncates=native > EMBED_DIM,
        )

    fallback: ModelRegistry | None = (
        db.query(ModelRegistry)
        .filter(
            ModelRegistry.model_type == "embedding",
            ModelRegistry.is_active.is_(True),
        )
        .order_by(ModelRegistry.id.asc())
        .first()
    )
    if fallback is None:
        return None
    logger.warning(
        "no is_platform_embedding designation — falling back to first "
        "active embedding model %r. Admin should designate one via "
        "POST /api/models/{id}/set-platform-embedding.",
        fallback.name,
    )
    native = _native_dim_of(fallback)
    return PlatformEmbedding(
        model=fallback,
        native_dim=native,
        truncates=native > EMBED_DIM,
    )


def count_pending_recompute(db: Session, designated_name: str | None) -> dict[str, int]:
    """Rows whose source model is not the current designation.

    NULL source counts as pending (legacy / pre-migration writes).
    """
    from sqlalchemy import text

    tables = (
        "conversation_memory_chunks",
        "document_chunks",
        "ingestion_images",
    )
    out: dict[str, int] = {}
    for table in tables:
        if designated_name is None:
            # Everything with an embedding is pending until designation.
            row = db.execute(
                text(
                    f"SELECT COUNT(*) FROM {table} "
                    f"WHERE embedding IS NOT NULL"
                )
            ).scalar()
        else:
            row = db.execute(
                text(
                    f"SELECT COUNT(*) FROM {table} "
                    f"WHERE embedding IS NOT NULL "
                    f"AND (embedding_source_model IS NULL "
                    f"     OR embedding_source_model != :name)"
                ),
                {"name": designated_name},
            ).scalar()
        out[table] = int(row or 0)
    out["total"] = sum(out[t] for t in tables)
    return out
