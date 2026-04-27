"""Shared fixtures for Sprint 1 G1 / G2 gate tests (Sprint 4 rebase).

These tests need a real pgvector + RLS-enabled Postgres. They connect
through the central anila-core PgPool, run through CollectionScopedPgVectorStore
end-to-end, and assert no collection ever sees another's chunks.

Sprint 4 rebase: fixtures provision 5 throwaway *collections* instead
of 5 agents (the agent abstraction was removed in migration 0019).
``isolation_collections`` is the entry-point fixture; documents are
provisioned alongside since chunks need a doc FK.

Skip strategy: every test in this directory is gated on
``INTEGRATION_DB_URL`` being set OR the host-side default
``postgresql://csp_app:csp@127.0.0.1:5432/csp`` being reachable.

Loop scope: every test in this directory is forced onto the session
event loop so session-scoped fixtures (PgPool + collection /
document rows) and the function-scoped tests share one loop.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

from anila_core.storage.adapters.pg_pool import PgPool


_DEFAULT_DSN = "postgresql://csp_app:csp@127.0.0.1:5432/csp"


def _resolve_dsn() -> str:
    return os.environ.get("INTEGRATION_DB_URL", _DEFAULT_DSN)


def _can_reach(dsn: str, timeout_s: float = 1.5) -> bool:
    """Quick TCP probe so a missing DB skips the suite cleanly."""

    async def _probe() -> bool:
        try:
            conn = await asyncpg.connect(dsn=dsn, timeout=timeout_s)
            await conn.close()
            return True
        except Exception:
            return False

    try:
        return asyncio.run(_probe())
    except Exception:
        return False


# ── Test collection hooks ───────────────────────────────────────────────────


def pytest_collection_modifyitems(config, items):
    """Two passes:
    1. Force every test under ``tests/integration/`` onto the session
       event loop (so session fixtures don't trip "different loop").
    2. Skip every test if the DB isn't reachable.
    """
    dsn = _resolve_dsn()
    reachable = bool(dsn) and _can_reach(dsn)
    skip_marker = pytest.mark.skip(
        reason=(
            f"pgvector not reachable at {dsn!r}; set INTEGRATION_DB_URL or "
            f"start the dev compose stack to enable G1/G2 gates."
        )
    )
    for item in items:
        if "integration" not in str(item.fspath):
            continue
        item.add_marker(pytest.mark.asyncio(loop_scope="session"))
        if not reachable:
            item.add_marker(skip_marker)


# ── Shared resources ────────────────────────────────────────────────────────


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def pool() -> AsyncIterator[PgPool]:
    """Session-scoped PgPool against the dev pgvector."""
    p = PgPool(_resolve_dsn(), min_size=1, max_size=4)
    await p.open()
    try:
        yield p
    finally:
        await p.close()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def isolation_collections(pool: PgPool) -> AsyncIterator[list[int]]:
    """Allocate 5 throwaway collections for the suite.

    Each collection's ``created_by`` points at the first existing user
    (CSP seed creates at least one admin). Cleanup at session-end
    deletes the collections (CASCADE clears every doc / chunk).
    """
    suffix = uuid.uuid4().hex[:8]
    coll_ids: list[int] = []

    conn = await asyncpg.connect(dsn=_resolve_dsn())
    try:
        owner_id = await conn.fetchval("SELECT id FROM users ORDER BY id LIMIT 1")
        if owner_id is None:
            raise RuntimeError(
                "No users in csp.users — CSP seed hasn't run; cannot allocate "
                "test collections for G1/G2."
            )
        for i in range(5):
            row = await conn.fetchrow(
                """
                INSERT INTO ingestion_collections
                    (name, chunking_config, embedding_model,
                     embedding_dim, created_by)
                VALUES ($1, '{"strategy":"fixed"}'::jsonb,
                        'test-model', 4000, $2)
                RETURNING id
                """,
                f"g1-coll-{i}-{suffix}",
                owner_id,
            )
            coll_ids.append(int(row["id"]))
    finally:
        await conn.close()

    yield coll_ids

    # Cleanup: delete the test collections; CASCADE clears chunks.
    try:
        conn2 = await asyncpg.connect(dsn=_resolve_dsn())
        try:
            await conn2.execute(
                "DELETE FROM ingestion_collections WHERE id = ANY($1::int[])",
                coll_ids,
            )
        finally:
            await conn2.close()
    except Exception:
        pass


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def isolation_documents(
    pool: PgPool, isolation_collections: list[int]
) -> AsyncIterator[list[int]]:
    """One placeholder document per collection (FK target for chunks)."""
    doc_ids: list[int] = []
    conn = await asyncpg.connect(dsn=_resolve_dsn())
    try:
        for coll_id in isolation_collections:
            row = await conn.fetchrow(
                """
                INSERT INTO ingestion_documents
                    (collection_id, filename, sha256, mime_type, status)
                VALUES ($1, 'g1-test.txt', $2, 'text/plain', 'indexed')
                RETURNING id
                """,
                coll_id,
                f"{coll_id:08d}" + "0" * 56,
            )
            doc_ids.append(int(row["id"]))
    finally:
        await conn.close()
    yield doc_ids
