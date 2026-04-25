"""asyncpg connection pool with pgvector codec registered.

Re-introduced in v0.6.0 (Phase 2 Sprint 1) after being removed in v0.5.0
boundary cleanup. The reason it's back: the central ingestion platform
ships ``AgentScopedPgVectorStore`` in anila-core, and that store needs a
shared pool.

Two responsibilities:

1. Wrap ``asyncpg.create_pool`` with sane defaults for the platform's
   workload (small min pool, modest max, generous timeouts so RLS-aware
   ``SET LOCAL`` setup never blocks).
2. On every connection acquired, register the pgvector ``vector`` codec
   so application code can pass ``list[float]`` and receive
   ``list[float]`` without manual SQL casting on every query.

We deliberately do NOT manage transactions or RLS GUC setup here — that
belongs to the consumer (``AgentScopedPgVectorStore._acquire``). Keeping
the pool dumb preserves the option to swap in pgbouncer / pgcat later.
"""

from __future__ import annotations

import asyncpg
from pgvector.asyncpg import register_vector


class PgPool:
    """Async connection pool with pgvector codec auto-registered.

    Construct with the connection string; call ``open()`` once during
    application startup, ``close()`` on shutdown. ``acquire()`` returns
    an asyncpg.Connection with the vector codec already registered.

    Designed for re-use across all anila-core consumers (the ingestion
    worker, evaluator runs, future memory store) — one pool per service
    instance, not one per request.
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

        Registering the vector codec here (not per-acquire) means each
        physical connection registers exactly once, even when reused
        across many ``acquire()`` calls. asyncpg caches the codec on
        the connection so subsequent queries go through it without
        per-statement overhead.
        """
        await register_vector(conn)
