"""Shared fixtures for Sprint 1 G1 / G2 gate tests.

These tests need a real pgvector + RLS-enabled Postgres. They connect
through the central anila-core PgPool, run through AgentScopedPgVectorStore
end-to-end, and assert no agent ever sees another's chunks.

Skip strategy: every test in this directory is gated on
``INTEGRATION_DB_URL`` being set OR the host-side default
``postgresql://csp_app:csp@127.0.0.1:5432/csp`` being reachable. CI
deploys the dev compose stack and points the env var at it; local dev
can ``docker compose up -d csp-db`` and run pytest directly.

Loop scope: every test in this directory is forced onto the session
event loop so session-scoped fixtures (PgPool + agent / collection /
document rows) and the function-scoped tests share one loop. Without
this, asyncpg raises "Future attached to a different loop" because
pytest-asyncio defaults each test to a fresh loop. We do the marker
injection in ``pytest_collection_modifyitems`` so individual test
files don't need ``pytestmark = pytest.mark.asyncio(loop_scope=...)``
boilerplate.

Cleanup: every fixture deletes the rows it created. The ``isolation_agents``
fixture allocates fresh agent rows per session via the superuser DSN
and tears them down (CASCADE) at the end.
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
_SUPERUSER_DSN_DEFAULT = "postgresql://csp:csp@127.0.0.1:5432/csp"


def _resolve_dsn() -> str:
    return os.environ.get("INTEGRATION_DB_URL", _DEFAULT_DSN)


def _resolve_superuser_dsn() -> str:
    """Superuser DSN — owns the ``agents`` table, so INSERTs there
    require ownership rather than just GRANT. csp_app owns it after
    migration 0014's ALTER TABLE OWNER loop, so the same role works.
    """
    return os.environ.get("INTEGRATION_SUPERUSER_DB_URL", _DEFAULT_DSN)


def _can_reach(dsn: str, timeout_s: float = 1.5) -> bool:
    """Quick TCP probe so a missing DB skips the suite cleanly without
    blowing up in fixture setup."""

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
    """Session-scoped PgPool against the dev pgvector.

    Pool size is small because tests are sequential — a wider pool
    masks contention bugs we want to surface.
    """
    p = PgPool(_resolve_dsn(), min_size=1, max_size=4)
    await p.open()
    try:
        yield p
    finally:
        await p.close()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def isolation_agents(pool: PgPool) -> AsyncIterator[list[int]]:
    """Allocate 5 throwaway agent rows for the suite.

    Cleanup at session-end CASCADEs every chunk / collection / document
    we leave behind.
    """
    su_dsn = _resolve_superuser_dsn()
    agent_ids: list[int] = []
    suffix = uuid.uuid4().hex[:8]
    # Borrow the first existing user as the agent owner; CSP installs at
    # least one admin user during seed so this is safe in dev.
    conn = await asyncpg.connect(dsn=su_dsn)
    try:
        owner_id = await conn.fetchval("SELECT id FROM users ORDER BY id LIMIT 1")
        if owner_id is None:
            raise RuntimeError(
                "No users in csp.users — CSP seed hasn't run; cannot allocate "
                "test agent rows for G1/G2."
            )
        for i in range(5):
            row = await conn.fetchrow(
                """
                INSERT INTO agents
                    (name, owner_user_id, endpoint_url)
                VALUES ($1, $2, 'http://test-noop')
                RETURNING id
                """,
                f"g1-test-{i}-{suffix}",
                owner_id,
            )
            agent_ids.append(int(row["id"]))
    finally:
        await conn.close()
    try:
        yield agent_ids
    finally:
        conn2 = await asyncpg.connect(dsn=su_dsn)
        try:
            await conn2.execute(
                "DELETE FROM agents WHERE id = ANY($1::int[])", agent_ids
            )
        finally:
            await conn2.close()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def isolation_collections(
    pool: PgPool, isolation_agents: list[int]
) -> AsyncIterator[list[int]]:
    """One collection per test agent."""
    su_dsn = _resolve_superuser_dsn()
    coll_ids: list[int] = []
    conn = await asyncpg.connect(dsn=su_dsn)
    try:
        for agent_id in isolation_agents:
            row = await conn.fetchrow(
                """
                INSERT INTO ingestion_collections
                    (agent_id, name, chunking_config, embedding_model,
                     embedding_dim)
                VALUES ($1, $2, '{"strategy":"fixed"}'::jsonb,
                        'test-model', 4000)
                RETURNING id
                """,
                agent_id,
                f"g1-coll-{agent_id}",
            )
            coll_ids.append(int(row["id"]))
    finally:
        await conn.close()
    yield coll_ids
    # Belt-and-suspenders: agent-row delete CASCADEs collections, but
    # if the agents fixture short-circuits (e.g. crash) we still try.
    try:
        conn2 = await asyncpg.connect(dsn=su_dsn)
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
    su_dsn = _resolve_superuser_dsn()
    doc_ids: list[int] = []
    conn = await asyncpg.connect(dsn=su_dsn)
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
                # 64-hex SHA-256 placeholder; collision-free across the
                # 5 collections via the coll-id prefix.
                f"{coll_id:08d}" + "0" * 56,
            )
            doc_ids.append(int(row["id"]))
    finally:
        await conn.close()
    yield doc_ids
