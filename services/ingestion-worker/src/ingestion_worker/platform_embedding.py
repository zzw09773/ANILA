"""Resolve the platform embedding designation for the ingestion worker.

Reads ``model_registry.is_platform_embedding`` from the shared CSP DB —
same source of truth as ``memory_service`` / collection defaults. Falls
back to ``WorkerSettings.embedding_model`` when nothing is designated so
a fresh deploy without an admin click still ingests.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Protocol

from anila_core.memory.long_term import EMBED_DIM

logger = logging.getLogger(__name__)


class _PoolLike(Protocol):
    def acquire(self): ...


@dataclass(frozen=True)
class ResolvedEmbedding:
    name: str
    native_dim: int

    @property
    def pad_from(self) -> Optional[int]:
        if self.native_dim == EMBED_DIM:
            return None
        return self.native_dim


async def resolve_from_pool(
    pool: _PoolLike,
    *,
    settings_fallback_name: str,
    settings_fallback_native: int | None = None,
) -> ResolvedEmbedding:
    """Look up the designated platform embedding; fall back to settings."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT name, embedding_native_dim
              FROM model_registry
             WHERE is_platform_embedding = true
               AND model_type = 'embedding'
               AND is_active = true
             LIMIT 1
            """
        )
        if row is not None:
            native = row["embedding_native_dim"]
            if not isinstance(native, int) or native <= 0:
                native = settings_fallback_native or EMBED_DIM
            return ResolvedEmbedding(name=row["name"], native_dim=native)

        # Soft fallback: first active embedding, then settings name.
        row = await conn.fetchrow(
            """
            SELECT name, embedding_native_dim
              FROM model_registry
             WHERE model_type = 'embedding'
               AND is_active = true
             ORDER BY id ASC
             LIMIT 1
            """
        )
        if row is not None:
            logger.warning(
                "ingestion-worker: no is_platform_embedding designation — "
                "using first active embedding %r",
                row["name"],
            )
            native = row["embedding_native_dim"]
            if not isinstance(native, int) or native <= 0:
                native = settings_fallback_native or EMBED_DIM
            return ResolvedEmbedding(name=row["name"], native_dim=native)

    logger.warning(
        "ingestion-worker: no embedding row in model_registry — "
        "falling back to settings name %r",
        settings_fallback_name,
    )
    return ResolvedEmbedding(
        name=settings_fallback_name,
        native_dim=settings_fallback_native or EMBED_DIM,
    )
