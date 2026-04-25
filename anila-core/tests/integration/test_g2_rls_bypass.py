"""Sprint 1 Gate G2 — raw-SQL bypass attempts blocked by RLS.

Per docs/ingestion-platform-design.md §3.3 Layer 2:

    DBA (or any caller using a regular asyncpg connection) cannot
    enumerate ``document_chunks`` rows by skipping the
    ``SET LOCAL anila.agent_id`` step. ``ENABLE ROW LEVEL SECURITY``
    + ``FORCE ROW LEVEL SECURITY`` + the ``chunks_agent_isolation``
    policy together must yield 0 rows when no scope is set, and only
    one agent's rows when a scope is set.

This complements G1: G1 proves the SDK enforces isolation; G2 proves
the engine still does even when the SDK is bypassed. Both layers must
hold for Sprint 1 to ship.

A handful of negative tests live here too — superuser DOES see
everything (as designed; that's how migrations run), and BYPASSRLS
roles bypass too. We assert ``csp_app`` has neither flag.
"""

from __future__ import annotations

import os

import asyncpg
import pytest

from anila_core.ingestion.chunking_plugins import ChunkResult
from anila_core.storage.adapters.pg_pool import PgPool
from anila_core.storage.adapters.pgvector_store import AgentScopedPgVectorStore


pytestmark = pytest.mark.asyncio


_DEFAULT_DSN = "postgresql://csp_app:csp@127.0.0.1:5432/csp"


def _resolve_dsn() -> str:
    return os.environ.get("INTEGRATION_DB_URL", _DEFAULT_DSN)


async def test_g2_csp_app_role_lacks_bypass_attributes(pool: PgPool) -> None:
    """``csp_app`` must NOT be superuser and must NOT have BYPASSRLS.

    Either flag voids RLS unconditionally — engine-side bypass before
    the policy even runs. Migration 0014 creates this role with both
    ``NOSUPERUSER`` and ``NOBYPASSRLS``; this test guards against a
    future ALTER ROLE that accidentally relaxes either.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname='csp_app'"
        )
    assert row is not None, "csp_app role missing — migration 0014 not applied?"
    assert row["rolsuper"] is False, "csp_app must NOT be superuser"
    assert row["rolbypassrls"] is False, "csp_app must NOT have BYPASSRLS"


async def test_g2_force_rls_enabled_on_document_chunks(pool: PgPool) -> None:
    """``relrowsecurity`` AND ``relforcerowsecurity`` must both be true.

    Without ``FORCE``, the table owner (currently csp_app per migration
    0014's ownership-transfer block) bypasses the policy. Migration 0014
    runs both ``ENABLE`` and ``FORCE``; this test guards regression.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT relrowsecurity, relforcerowsecurity
              FROM pg_class
             WHERE relname = 'document_chunks'
            """
        )
    assert row is not None
    assert row["relrowsecurity"] is True, "RLS not enabled on document_chunks"
    assert row["relforcerowsecurity"] is True, (
        "FORCE RLS not enabled — table owner can bypass the isolation policy"
    )


async def test_g2_bypass_attempt_no_guc_yields_zero_rows(
    pool: PgPool,
    isolation_agents: list[int],
    isolation_documents: list[int],
) -> None:
    """A raw connection without ``SET LOCAL anila.agent_id`` must see 0 rows.

    Pre-condition: G1's insert phase already populated chunks for the
    test agents. We open a fresh raw asyncpg connection (NOT through
    AgentScopedPgVectorStore), do not set the GUC, and ``SELECT *``
    — the policy's USING clause cannot bind ``current_setting`` and
    the engine returns 0 rows. This is the headline "no scope = no
    visibility" property.
    """
    # Insert one chunk for the first test agent so the table isn't
    # empty for unrelated reasons (G1 may have run before us in the
    # same session, but be defensive — fixture ordering changes).
    agent_id = isolation_agents[0]
    doc_id = isolation_documents[0]
    coll_id = await _coll_for_agent(pool, agent_id)
    store = AgentScopedPgVectorStore(pool, agent_id=agent_id)
    await store.index_chunks(
        collection_id=coll_id,
        document_id=doc_id,
        chunks=[
            ChunkResult(
                content="g2 secret",
                chunk_key=f"g2-bypass-{agent_id}",
                token_count=2,
                metadata={"test": "g2"},
            )
        ],
        embeddings=[[0.1] * 4000],
    )

    raw_conn = await asyncpg.connect(dsn=_resolve_dsn())
    try:
        rows = await raw_conn.fetch("SELECT * FROM document_chunks")
        assert rows == [], (
            f"G2 BREACH: raw asyncpg connection without anila.agent_id GUC "
            f"saw {len(rows)} rows. RLS USING clause failed to filter."
        )
    finally:
        await raw_conn.close()


async def test_g2_bypass_attempt_wrong_agent_yields_only_their_rows(
    pool: PgPool,
    isolation_agents: list[int],
    isolation_documents: list[int],
) -> None:
    """Set GUC to agent A → only A's rows visible. Manual SELECT cannot
    cheat the predicate even on a connection the operator controls."""
    agent_a = isolation_agents[0]
    agent_b = isolation_agents[1]

    # Make sure both agents have at least one row each.
    coll_a = await _coll_for_agent(pool, agent_a)
    coll_b = await _coll_for_agent(pool, agent_b)
    doc_a = isolation_documents[0]
    doc_b = isolation_documents[1]

    for store, coll_id, doc_id, label in (
        (AgentScopedPgVectorStore(pool, agent_id=agent_a), coll_a, doc_a, "A"),
        (AgentScopedPgVectorStore(pool, agent_id=agent_b), coll_b, doc_b, "B"),
    ):
        await store.index_chunks(
            collection_id=coll_id,
            document_id=doc_id,
            chunks=[
                ChunkResult(
                    content=f"g2 {label}",
                    chunk_key=f"g2-pin-{label}-{store.agent_id}",
                    token_count=1,
                )
            ],
            embeddings=[[0.2] * 4000],
        )

    raw_conn = await asyncpg.connect(dsn=_resolve_dsn())
    try:
        async with raw_conn.transaction():
            await raw_conn.execute(f"SET LOCAL anila.agent_id = {agent_a}")
            rows = await raw_conn.fetch("SELECT agent_id FROM document_chunks")
        offenders = [r["agent_id"] for r in rows if r["agent_id"] != agent_a]
        assert not offenders, (
            f"G2 BREACH: with anila.agent_id={agent_a}, raw query also "
            f"returned rows for agents {offenders[:5]}"
        )
        assert any(r["agent_id"] == agent_a for r in rows), (
            "Sanity: agent A must see its own rows"
        )
    finally:
        await raw_conn.close()


# ── Helpers ─────────────────────────────────────────────────────────────────


async def _coll_for_agent(pool: PgPool, agent_id: int) -> int:
    """Look up the test collection owned by this agent.

    The ``isolation_collections`` fixture creates one per agent; we
    lookup by FK rather than passing the list around because the
    ordering of the list is by agent_id ascending and we want to keep
    G2 tests independent of fixture wiring details.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM ingestion_collections WHERE agent_id = $1 LIMIT 1",
            agent_id,
        )
    assert row is not None, (
        f"No test collection found for agent {agent_id} — fixture order broken?"
    )
    return int(row["id"])
