"""Resolve the platform embedding designation for the ingestion worker.

The model name comes only from ``model_registry.is_platform_embedding``.
Nothing here reads ``EMBEDDING_MODEL`` or picks another row.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from anila_core.memory.long_term import EMBED_DIM


class EmbeddingRoleUnset(RuntimeError):
    """治理中心還沒指定平台嵌入模型。"""


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
    """Look up the designated platform embedding.

    ``settings_fallback_*`` is accepted so older callers keep working, and
    is not used as a model name.
    """
    del settings_fallback_name, settings_fallback_native
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
    if row is None:
        raise EmbeddingRoleUnset("平台嵌入模型尚未在治理中心設定")
    native = row["embedding_native_dim"]
    if not isinstance(native, int) or native <= 0:
        native = EMBED_DIM
    return ResolvedEmbedding(name=row["name"], native_dim=native)
