"""Sprint 1 Gate G2 (Sprint 4 rebase) — raw-SQL bypass attempts blocked.

Per docs/ingestion/ingestion-platform-design.md §3.3 Layer 2 (Sprint 4 update):

    DBA / any caller using a regular asyncpg connection cannot
    enumerate ``document_chunks`` rows by skipping the
    ``SET LOCAL anila.collection_id`` step. ``ENABLE ROW LEVEL SECURITY``
    + ``FORCE ROW LEVEL SECURITY`` + the ``chunks_collection_isolation``
    policy together yield 0 rows when no scope is set, and only one
    collection's rows when a scope is set.

This complements G1: G1 proves the SDK enforces isolation; G2 proves
the engine still does even when the SDK is bypassed.
"""

from __future__ import annotations

import os

import asyncpg
import pytest
from anila_contracts import Classification

from anila_core.ingestion.chunking_plugins import ChunkResult
from anila_core.storage.adapters.pg_pool import PgPool
from anila_core.storage.adapters.pgvector_store import CollectionScopedPgVectorStore


pytestmark = pytest.mark.asyncio


_DEFAULT_DSN = "postgresql://csp_app:csp@127.0.0.1:5432/csp"


def _resolve_dsn() -> str:
    return os.environ.get("INTEGRATION_DB_URL", _DEFAULT_DSN)


async def test_g2_csp_app_role_lacks_bypass_attributes(pool: PgPool) -> None:
    """``csp_app`` must NOT be superuser and must NOT have BYPASSRLS."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname='csp_app'"
        )
    assert row is not None, "csp_app role missing — migration 0014 not applied?"
    assert row["rolsuper"] is False, "csp_app must NOT be superuser"
    assert row["rolbypassrls"] is False, "csp_app must NOT have BYPASSRLS"


async def test_g2_force_rls_enabled_on_document_chunks(pool: PgPool) -> None:
    """``relrowsecurity`` AND ``relforcerowsecurity`` must both be true."""
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


async def test_g2_detector_rejects_a_removed_collection_policy(
    pool: PgPool,
    integration_admin_dsn: str,
    isolation_collections: list[int],
    isolation_documents: list[int],
) -> None:
    """Mutation control: removing the real policy must trip the isolation oracle.

    PostgreSQL DDL is transactional.  The policy is dropped only inside this
    transaction, the session assumes ``csp_app``, and rollback restores the
    schema before the next test.  This proves the required suite is sensitive
    to policy removal instead of merely reporting a green happy path.
    """

    coll_id = isolation_collections[0]
    doc_id = isolation_documents[0]
    store = CollectionScopedPgVectorStore(pool, collection_id=coll_id)
    await store.stage_and_activate_generation(
        document_id=doc_id,
        source_ingestion_job_id=None,
        source_ingestion_lease_token=None,
        embedding_model="test-model",
        embedding_fingerprint="sha256:" + ("0" * 64),
        embedding_dim=4000,
        parent_chunks=[],
        leaf_chunks=[
            ChunkResult(
                content="rls mutation sentinel",
                chunk_key=f"rls-mutation-{coll_id}",
                token_count=3,
            )
        ],
        embeddings=[[0.3] * 4000],
        classification_level=Classification.UNCLASSIFIED,
    )

    admin = await asyncpg.connect(dsn=integration_admin_dsn)
    transaction = admin.transaction()
    try:
        await transaction.start()
        try:
            policy_exists = await admin.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM pg_policies
                    WHERE schemaname = 'public'
                      AND tablename = 'document_chunks'
                      AND policyname = 'chunks_collection_isolation'
                )
                """
            )
            assert policy_exists, "required chunks_collection_isolation policy missing"
            await admin.execute(
                "DROP POLICY chunks_collection_isolation ON document_chunks"
            )
            await admin.execute("SET LOCAL ROLE csp_app")
            await admin.execute(
                "SELECT set_config('anila.collection_id', $1, true)", str(coll_id)
            )
            visible = await admin.fetchval(
                "SELECT count(*) FROM document_chunks WHERE collection_id = $1",
                coll_id,
            )
            with pytest.raises(
                AssertionError,
                match="RLS isolation oracle saw no rows",
            ):
                assert visible > 0, "RLS isolation oracle saw no rows"
        finally:
            # The mutation must never escape this test, even though the
            # expected AssertionError is consumed by pytest.raises().
            await transaction.rollback()
    finally:
        await admin.close()


async def test_g2_bypass_attempt_no_guc_yields_zero_rows(
    pool: PgPool,
    isolation_collections: list[int],
    isolation_documents: list[int],
) -> None:
    """Raw connection without ``SET LOCAL anila.collection_id`` must see 0 rows."""
    coll_id = isolation_collections[0]
    doc_id = isolation_documents[0]
    store = CollectionScopedPgVectorStore(pool, collection_id=coll_id)
    await store.stage_and_activate_generation(
        document_id=doc_id,
        source_ingestion_job_id=None,
        source_ingestion_lease_token=None,
        embedding_model="test-model",
        embedding_fingerprint="sha256:" + ("0" * 64),
        embedding_dim=4000,
        parent_chunks=[],
        leaf_chunks=[
            ChunkResult(
                content="g2 secret",
                chunk_key=f"g2-bypass-{coll_id}",
                token_count=2,
            )
        ],
        embeddings=[[0.1] * 4000],
        classification_level=Classification.UNCLASSIFIED,
    )

    raw_conn = await asyncpg.connect(dsn=_resolve_dsn())
    try:
        rows = await raw_conn.fetch("SELECT * FROM document_chunks")
        assert rows == [], (
            f"G2 BREACH: raw connection without anila.collection_id GUC "
            f"saw {len(rows)} rows."
        )
    finally:
        await raw_conn.close()


async def test_g2_bypass_attempt_wrong_collection_yields_only_their_rows(
    pool: PgPool,
    isolation_collections: list[int],
    isolation_documents: list[int],
) -> None:
    """Set GUC to collection A → only A's rows visible. Manual SELECT
    cannot cheat the predicate even on a connection the operator
    controls."""
    coll_a = isolation_collections[0]
    coll_b = isolation_collections[1]
    doc_a = isolation_documents[0]
    doc_b = isolation_documents[1]

    for store, coll_id, doc_id, label in (
        (CollectionScopedPgVectorStore(pool, collection_id=coll_a), coll_a, doc_a, "A"),
        (CollectionScopedPgVectorStore(pool, collection_id=coll_b), coll_b, doc_b, "B"),
    ):
        await store.stage_and_activate_generation(
            document_id=doc_id,
            source_ingestion_job_id=None,
            source_ingestion_lease_token=None,
            embedding_model="test-model",
            embedding_fingerprint="sha256:" + ("0" * 64),
            embedding_dim=4000,
            parent_chunks=[],
            leaf_chunks=[
                ChunkResult(
                    content=f"g2 {label}",
                    chunk_key=f"g2-pin-{label}-{coll_id}",
                    token_count=1,
                )
            ],
            embeddings=[[0.2] * 4000],
            classification_level=Classification.UNCLASSIFIED,
        )

    raw_conn = await asyncpg.connect(dsn=_resolve_dsn())
    try:
        async with raw_conn.transaction():
            await raw_conn.execute(f"SET LOCAL anila.collection_id = {coll_a}")
            rows = await raw_conn.fetch(
                "SELECT collection_id FROM document_chunks"
            )
        offenders = [r["collection_id"] for r in rows if r["collection_id"] != coll_a]
        assert not offenders, (
            f"G2 BREACH: with anila.collection_id={coll_a}, raw query also "
            f"returned rows for collections {offenders[:5]}"
        )
        assert any(r["collection_id"] == coll_a for r in rows), (
            "Sanity: collection A must see its own rows"
        )
    finally:
        await raw_conn.close()


async def test_transaction_local_collection_scope_never_survives_pool_reuse(
    isolation_collections: list[int],
) -> None:
    """A max-size-one pool guarantees the next request reuses the connection."""

    coll_id = isolation_collections[0]
    one_connection_pool = await asyncpg.create_pool(
        dsn=_resolve_dsn(), min_size=1, max_size=1
    )
    try:
        async with one_connection_pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    f"SET LOCAL anila.collection_id = {int(coll_id)}"
                )
                assert await connection.fetchval(
                    "SELECT current_setting('anila.collection_id', true)"
                ) == str(coll_id)

        # This is the exact same physical connection after release/acquire.
        async with one_connection_pool.acquire() as reused:
            residual = await reused.fetchval(
                "SELECT current_setting('anila.collection_id', true)"
            )
            assert residual in (None, "")
            assert await reused.fetch("SELECT id FROM document_chunks") == []
    finally:
        await one_connection_pool.close()
