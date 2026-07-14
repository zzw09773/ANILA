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
from urllib.parse import urlsplit

import asyncpg
import pytest
import pytest_asyncio

from anila_core.storage.adapters.pg_pool import PgPool


_DEFAULT_DSN = "postgresql://csp_app:csp@127.0.0.1:5432/csp"
_REQUIRE_RLS = os.environ.get("ANILA_REQUIRE_RLS_TESTS", "").lower() in {
    "1", "true", "yes",
}


def _resolve_dsn() -> str:
    return os.environ.get("INTEGRATION_DB_URL", _DEFAULT_DSN)


def _safe_dsn_label(dsn: str) -> str:
    """Describe a target without ever echoing CI database credentials."""

    try:
        parsed = urlsplit(dsn)
        host = parsed.hostname or "configured-host"
        port = f":{parsed.port}" if parsed.port else ""
        database = parsed.path or "/configured-db"
        return f"{parsed.scheme or 'postgresql'}://{host}{port}{database}"
    except (TypeError, ValueError):
        return "<configured PostgreSQL DSN>"


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
    if _REQUIRE_RLS and not reachable:
        raise pytest.UsageError(
            "ANILA_REQUIRE_RLS_TESTS=1 but the real PostgreSQL app-role DSN "
            f"is unreachable: {_safe_dsn_label(dsn)}"
        )
    skip_marker = pytest.mark.skip(
        reason=(
            f"pgvector not reachable at {_safe_dsn_label(dsn)}; set INTEGRATION_DB_URL or "
            f"start the dev compose stack to enable G1/G2 gates."
        )
    )
    for item in items:
        if "integration" not in str(item.fspath):
            continue
        item.add_marker(pytest.mark.asyncio(loop_scope="session"))
        if not reachable:
            item.add_marker(skip_marker)


@pytest.fixture(scope="session")
def integration_admin_dsn() -> str:
    """Migration/superuser DSN used only for RLS metadata mutation controls."""

    dsn = os.environ.get("INTEGRATION_ADMIN_DB_URL")
    if dsn:
        return dsn
    if _REQUIRE_RLS:
        raise pytest.UsageError(
            "ANILA_REQUIRE_RLS_TESTS=1 requires INTEGRATION_ADMIN_DB_URL"
        )
    pytest.skip("INTEGRATION_ADMIN_DB_URL is required for RLS mutation controls")


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
async def integration_owner_id() -> AsyncIterator[int]:
    """Return an owner row, provisioning a disabled test user on a fresh DB.

    Gate 1 CI starts from Alembic ``head`` without running CSP auto-seed.  The
    RLS suite must therefore own its prerequisite instead of depending on an
    operator/admin account.  The synthetic row cannot authenticate and is
    removed after all dependent collections have been cleaned up.
    """

    conn = await asyncpg.connect(dsn=_resolve_dsn())
    created_id: int | None = None
    try:
        owner_id = await conn.fetchval("SELECT id FROM users ORDER BY id LIMIT 1")
        if owner_id is None:
            owner_id = await conn.fetchval(
                """
                INSERT INTO users
                    (username, hashed_password, role, is_active, is_approved,
                     local_password_disabled, ui_settings)
                VALUES ($1, $2, 'user', false, false, true, '{}'::jsonb)
                RETURNING id
                """,
                f"rls-integration-{uuid.uuid4().hex}",
                "!disabled-gate1-rls-fixture!",
            )
            created_id = int(owner_id)
    finally:
        await conn.close()

    yield int(owner_id)

    if created_id is not None:
        try:
            cleanup = await asyncpg.connect(dsn=_resolve_dsn())
            try:
                await cleanup.execute("DELETE FROM users WHERE id = $1", created_id)
            finally:
                await cleanup.close()
        except Exception:
            pass


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def isolation_collections(
    pool: PgPool, integration_owner_id: int
) -> AsyncIterator[list[int]]:
    """Allocate 5 throwaway collections for the suite.

    Each collection's ``created_by`` points at the suite-owned owner fixture.
    Cleanup at session-end deletes the collections (CASCADE clears every doc /
    chunk).
    """
    suffix = uuid.uuid4().hex[:8]
    coll_ids: list[int] = []

    conn = await asyncpg.connect(dsn=_resolve_dsn())
    try:
        for i in range(5):
            row = await conn.fetchrow(
                """
                INSERT INTO ingestion_collections
                    (name, chunking_config, embedding_model,
                     embedding_fingerprint, embedding_dim, created_by)
                VALUES ($1, '{"strategy":"fixed"}'::jsonb,
                        'test-model', $2, 4000, $3)
                RETURNING id
                """,
                f"g1-coll-{i}-{suffix}",
                "sha256:" + ("0" * 64),
                integration_owner_id,
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
