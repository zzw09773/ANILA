"""asyncpg connection pool with pgvector codec registered.

Re-introduced into AgenticRAG during Phase 0 decoupling (2026-05-02).
Originally lived here in v0.5, was removed in v0.6 / Sprint 1 Chunk F
when the platform centralised it into anila-core. AgenticRAG now owns
its own copy again so the fork-template promise (``pip install
agentic-rag[rag]`` works without anila-core) holds.

Two responsibilities:

1. Wrap ``asyncpg.create_pool`` with sane defaults for typical RAG
   workloads (small min, modest max, generous timeouts so RLS-aware
   ``SET LOCAL`` setup never blocks).
2. On every connection acquired, register pgvector / jsonb codecs so
   application code can pass ``list[float]`` and receive
   ``list[float]`` without per-query SQL casting, and read JSONB
   columns as ``dict`` directly.

We deliberately do NOT manage transactions or RLS GUC setup here —
that belongs to the consumer (``CollectionScopedPgVectorStore.
_acquire``). Keeping the pool dumb preserves the option to swap in
pgbouncer / pgcat later.
"""

from __future__ import annotations

import json

import asyncpg
from pgvector.asyncpg import register_vector


class PgPool:
    """Async connection pool with pgvector codec auto-registered.

    Construct with the connection string; call ``open()`` once during
    application startup, ``close()`` on shutdown. ``acquire()`` returns
    an asyncpg.Connection with the vector codec already registered.

    Designed for re-use across consumers in one service instance — one
    pool per process, not one per request.
    """

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
        command_timeout: float = 30.0,
    ) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._command_timeout = command_timeout
        self._pool: asyncpg.Pool | None = None

    async def open(self) -> None:
        """Create the pool. Idempotent: safe to call twice."""
        if self._pool is not None:
            return
        self._pool = await asyncpg.create_pool(
            dsn=self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            command_timeout=self._command_timeout,
            init=self._init_connection,
        )

    async def close(self) -> None:
        """Close the pool. Idempotent."""
        if self._pool is None:
            return
        await self._pool.close()
        self._pool = None

    def acquire(self) -> asyncpg.pool.PoolAcquireContext:
        """Acquire a connection. Raises if ``open()`` was not called."""
        if self._pool is None:
            raise RuntimeError(
                "PgPool not opened. Call await pool.open() during startup."
            )
        return self._pool.acquire()

    @staticmethod
    async def _init_connection(conn: asyncpg.Connection) -> None:
        """Per-connection initialiser invoked by asyncpg.

        Two codecs to register, both off by default in asyncpg:

        - ``vector`` (pgvector): list[float] ↔ pgvector value.
        - ``jsonb``: dict ↔ jsonb. Without this, JSONB columns come
          back as raw JSON strings; every consumer would have to call
          ``json.loads`` on each read. Centralising the codec avoids
          the gotcha.
        """
        await register_vector(conn)
        await conn.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )
