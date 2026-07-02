"""Module-level asyncpg pool for the ingestion inspector endpoints.

The CSP backend's main DB access is via SQLAlchemy (sync). The
inspector wants to use the central ``AgentScopedPgVectorStore`` which
requires an async asyncpg pool — different driver, different lifecycle.
Rather than awkwardly mixing the two on every request, we open one
shared async pool at app lifespan startup and close it at shutdown.

The pool DSN comes from ``DATABASE_URL`` (the runtime ``csp_app`` role)
so RLS still enforces the agent scope inside ``SET LOCAL`` blocks.
"""

from __future__ import annotations

import logging
import os

from anila_core.storage.adapters.pg_pool import PgPool


logger = logging.getLogger(__name__)


_pool: PgPool | None = None


async def open_pool() -> None:
    """Open the singleton pool. Idempotent.

    Reads ``DATABASE_URL`` directly from env to bypass the SQLAlchemy
    URL transforms that have to happen for psycopg2 — asyncpg parses
    the same DSN cleanly without alteration.
    """
    global _pool
    if _pool is not None:
        return
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL not set; ingestion pool needs an asyncpg DSN."
        )
    _pool = PgPool(dsn, min_size=1, max_size=8)
    await _pool.open()
    logger.info("Ingestion PgPool opened (csp_app runtime, RLS-enforced)")


async def close_pool() -> None:
    """Close the singleton pool. Safe to call when never opened."""
    global _pool
    if _pool is None:
        return
    await _pool.close()
    _pool = None


def get_pool() -> PgPool:
    """Return the open pool. Raises if lifespan didn't successfully open one.

    Endpoints depending on the inspector pool should catch this and
    return 503 Service Unavailable so the front-end can render
    "ingestion temporarily disabled" rather than a 500.
    """
    if _pool is None:
        raise RuntimeError(
            "Ingestion pool not initialised. Either app lifespan failed "
            "to open it (check startup logs) or this is being called "
            "outside an active app — tests should use the integration "
            "pool fixture."
        )
    return _pool
