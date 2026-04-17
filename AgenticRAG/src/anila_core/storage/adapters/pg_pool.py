"""Shared asyncpg connection pool manager.

All PostgreSQL adapters (pgvector_store, postgres_store) share a single pool
to avoid redundant connections. The pool is lazily initialized on first use
and torn down via close().

Usage:
    pool = PgPool(dsn="postgresql://user:pass@host/db")
    await pool.initialize()
    async with pool.acquire() as conn:
        await conn.execute("SELECT 1")
    await pool.close()
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


class PgPool:
    """Wrapper around asyncpg.Pool with lazy initialization.

    Args:
        dsn:         PostgreSQL DSN string.
        min_size:    Minimum pool connections.
        max_size:    Maximum pool connections.
        ssl:         SSL mode string ('disable', 'require', 'verify-ca', etc.)
        timeout:     Connection acquisition timeout in seconds.
    """

    def __init__(
        self,
        dsn: str,
        min_size: int = 2,
        max_size: int = 10,
        ssl: str = "disable",
        timeout: float = 30.0,
    ) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._ssl = ssl
        self._timeout = timeout
        self._pool: Any = None  # asyncpg.Pool

    async def initialize(self) -> None:
        """Create the connection pool. Idempotent."""
        if self._pool is not None:
            return
        try:
            import asyncpg  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "asyncpg is required for PostgreSQL storage. "
                "Install with: pip install 'anila-core[rag]'"
            ) from exc

        ssl_context: Any = None
        if self._ssl not in ("disable", "prefer", "allow"):
            import ssl as ssl_module
            ssl_context = ssl_module.create_default_context()
            if self._ssl == "require":
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl_module.CERT_NONE

        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            ssl=ssl_context,
            command_timeout=self._timeout,
        )
        logger.info("asyncpg pool created (min=%d, max=%d)", self._min_size, self._max_size)

    async def close(self) -> None:
        """Close all connections in the pool."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            logger.info("asyncpg pool closed")

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[Any]:
        """Acquire a connection from the pool as an async context manager."""
        if self._pool is None:
            await self.initialize()
        async with self._pool.acquire() as conn:
            yield conn

    @property
    def is_initialized(self) -> bool:
        return self._pool is not None
