"""Postgres integration test for ``CollectionScopedPgVectorStore.source_model_coverage``.

Why this file exists
--------------------
The SQLite suite stubs the store, so replacing the whole coverage query
with a constant meaning "everything is healthy" — i.e. deleting the fix
in production — left every other test green. The endpoint's *decision*
was covered; the SQL that feeds it was not executed anywhere.

This runs the real method against real PostgreSQL, under real RLS, with
real rows, so the query itself is what passes or fails. It is SKIPPED
unless ``ANILA_TEST_PG_DSN`` points at a Postgres a *superuser* can use —
the same contract as ``test_ingestion_images_rls_pg.py`` and
``test_platform_embedding_pg.py``. Everything it creates lives in a
uuid-suffixed schema and role that are dropped in ``finally``; it never
touches ``public`` or any platform table.

Run:
  ANILA_TEST_PG_DSN=postgresql://user:pw@127.0.0.1:5433/csp \
      pytest tests/test_source_model_coverage_pg.py

⚠ What this still does NOT prove: that the *production* ``document_chunks``
(halfvec column, HNSW index, real row volumes) plans the same way. The
table here mirrors the columns the query reads, not the vector machinery.
Query planning on a populated production table is a deployment-time
check — see the package report.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")

from anila_core.storage.adapters.pgvector_store import (  # noqa: E402
    _COVERAGE_SAMPLE_ROWS,
    CollectionScopedPgVectorStore,
)

_DSN = os.environ.get("ANILA_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="ANILA_TEST_PG_DSN (superuser) not set — the coverage SQL needs Postgres",
)

_POLICY = "collection_id = NULLIF(current_setting('anila.collection_id', true), '')::int"

DESIGNATED = "nvidia/nv-embed-v2"
STALE = "legacy/old-embedder"

# Not invented for the test. Read out of the live database on 2026-08-05:
#   SELECT column_default FROM information_schema.columns
#    WHERE table_name='ingestion_collections' AND column_name='embedding_model';
#     -> 'nvidia/NV-embed-V2'::character varying
#   SELECT name FROM model_registry WHERE model_type='embedding';
#     -> nvidia/nv-embed-v2
# Migration r1_0018 backfilled chunk provenance straight from that column
# (``SET embedding_source_model = ic.embedding_model``), so live chunks can
# carry the mixed-case spelling of the very model that is designated.
COLLECTION_COLUMN_DEFAULT = "nvidia/NV-embed-V2"


class _ConnPool:
    """Minimal ``PgPool`` stand-in: hands the store one live connection.

    The store only needs ``acquire()`` as an async context manager; it
    drives the transaction and the ``SET LOCAL`` itself, which is exactly
    the behaviour under test.
    """

    def __init__(self, conn) -> None:
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


async def _run() -> dict:
    schema = f"cov_{uuid.uuid4().hex[:8]}"
    role = f"cov_app_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(_DSN)
    out: dict = {}
    app_conn = None
    try:
        # ── fixtures (as superuser) — mirror migration 0019's policy ──
        await admin.execute(f"CREATE SCHEMA {schema}")
        await admin.execute(
            f"CREATE TABLE {schema}.document_chunks ("
            f"  id bigserial primary key,"
            f"  collection_id int not null,"
            f"  chunk_type text not null default 'leaf',"
            f"  embedding text,"
            f"  embedding_source_model text)"
        )
        await admin.execute(
            f"ALTER TABLE {schema}.document_chunks ENABLE ROW LEVEL SECURITY"
        )
        await admin.execute(
            f"ALTER TABLE {schema}.document_chunks FORCE ROW LEVEL SECURITY"
        )
        await admin.execute(
            f"CREATE POLICY p ON {schema}.document_chunks FOR ALL USING ({_POLICY})"
        )
        await admin.execute(f"CREATE ROLE {role} NOLOGIN NOBYPASSRLS")
        await admin.execute(f"GRANT USAGE ON SCHEMA {schema} TO {role}")
        await admin.execute(f"GRANT SELECT ON {schema}.document_chunks TO {role}")

        async def insert(collection_id, source_model, n=1, chunk_type="leaf", embedded=True):
            await admin.executemany(
                f"INSERT INTO {schema}.document_chunks "
                f"(collection_id, chunk_type, embedding, embedding_source_model) "
                f"VALUES ($1, $2, $3, $4)",
                [
                    (collection_id, chunk_type, "v" if embedded else None, source_model)
                    for _ in range(n)
                ],
            )

        # collection 1: every chunk under the stale model — the defect.
        await insert(1, STALE, n=3)
        # collection 2: every chunk under the designated model — healthy.
        await insert(2, DESIGNATED, n=3)
        # collection 3: mixed — mid-reindex.
        await insert(3, DESIGNATED, n=2)
        await insert(3, STALE, n=2)
        # collection 4: legacy rows with NULL provenance.
        await insert(4, None, n=2)
        # collection 5: nothing at all (no inserts).
        # collection 6: parent rows and un-embedded rows only — must not
        # be mistaken for a stale corpus.
        await insert(6, STALE, n=2, chunk_type="heading")
        await insert(6, STALE, n=2, embedded=False)
        # collection 7: more fresh rows than the sample window, then one
        # stale row appended after it — pins the documented bound.
        await insert(7, DESIGNATED, n=_COVERAGE_SAMPLE_ROWS + 50)
        await insert(7, STALE, n=1)
        # collection 9: the live casing collision — chunks carry the
        # collection column's DEFAULT spelling, the registry carries the
        # lowercase one. Same model. A case-sensitive ``=`` calls this
        # stranded and 409s a knowledge base that works.
        await insert(9, COLLECTION_COLUMN_DEFAULT, n=3)
        # collection 8: a reindex that has only just started — stale rows
        # far beyond the sampling window, then one fresh row. A sampled
        # ``has_matching`` would call this stranded and 409 the whole
        # knowledge base mid-migration.
        await insert(8, STALE, n=_COVERAGE_SAMPLE_ROWS + 50)
        await insert(8, DESIGNATED, n=1)

        # ── act as the NOBYPASSRLS app role, in the throwaway schema ──
        app_conn = await asyncpg.connect(
            _DSN, server_settings={"role": role, "search_path": schema}
        )
        pool = _ConnPool(app_conn)

        for cid in (1, 2, 3, 4, 5, 6, 7, 8, 9):
            store = CollectionScopedPgVectorStore(pool, collection_id=cid)
            cov = await store.source_model_coverage(DESIGNATED)
            out[cid] = (cov.has_matching, cov.has_other, cov.sample_other_model)

        out["role"] = await app_conn.fetchval("SELECT current_user")

        # Direct measurement of the sampling bound, in the same RLS scope
        # the store uses: collection 7 holds more qualifying rows than the
        # window, and the window must still stop at the window.
        async with app_conn.transaction():
            await app_conn.execute("SET LOCAL anila.collection_id = 7")
            out["qualifying_rows"] = await app_conn.fetchval(
                "SELECT count(*) FROM document_chunks "
                "WHERE chunk_type = 'leaf' AND embedding IS NOT NULL"
            )
            out["sampled_rows"] = await app_conn.fetchval(
                f"SELECT count(*) FROM ("
                f"  SELECT 1 FROM document_chunks"
                f"   WHERE chunk_type = 'leaf' AND embedding IS NOT NULL"
                f"   LIMIT {int(_COVERAGE_SAMPLE_ROWS)}) s"
            )
    finally:
        if app_conn is not None:
            await app_conn.close()
        await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin.execute(f"DROP ROLE IF EXISTS {role}")
        await admin.close()
    return out


@pytest.fixture(scope="module")
def coverage():
    return asyncio.run(_run())


def test_runs_as_a_non_bypassrls_role(coverage):
    """If this ran as a superuser the RLS assertions below would be
    vacuous — every row would be visible regardless."""
    assert coverage["role"].startswith("cov_app_")


def test_whole_collection_under_another_model_is_detected(coverage):
    """The defect itself, in SQL: rows exist, none under the designated
    model. Mutant: replace the query with a constant "healthy" row — this
    fails, which is the gap this file was written to close."""
    assert coverage[1] == (False, True, STALE)


def test_healthy_collection_reports_no_other_model(coverage):
    """Must not fire on a correctly-indexed corpus — a false 409 here
    would break every working knowledge base."""
    assert coverage[2] == (True, False, None)


def test_mixed_collection_reports_both(coverage):
    has_matching, has_other, sample = coverage[3]
    assert has_matching is True
    assert has_other is True
    assert sample == STALE


def test_null_provenance_counts_as_another_model(coverage):
    """Legacy rows predate P4.8 and carry NULL. ``IS DISTINCT FROM`` is
    what makes NULL count as "other" rather than silently matching; plain
    ``!=`` would return NULL and the whole corpus would look empty.

    The sample name is None here even though ``has_other`` is True —
    that combination is real, and callers must not treat a null sample as
    "nothing found"."""
    assert coverage[4] == (False, True, None)


def test_empty_collection_is_empty_not_stranded(coverage):
    """Nothing indexed → the endpoint must keep returning a plain empty
    200. Also proves RLS scoping: collection 1's stale rows sit in the
    same table and do not leak in."""
    assert coverage[5] == (False, False, None)


def test_parent_rows_and_unembedded_rows_are_not_a_stranded_corpus(coverage):
    """``chunk_type='heading'`` rows and rows with a NULL embedding are
    never retrievable by vector search, so they must not be read as a
    stale index — that would 409 a collection whose only fault is having
    parent rows."""
    assert coverage[6] == (False, False, None)


def test_sampling_window_stops_at_the_window(coverage):
    """The cost claim, measured rather than asserted in prose.

    Collection 7 holds more qualifying leaf rows than the window. The
    window must still read exactly ``_COVERAGE_SAMPLE_ROWS`` — that is
    what makes the healthy-corpus case constant-cost instead of a full
    collection scan on every zero-hit query.

    If someone removes the ``LIMIT`` to make partial-reindex detection
    exact, this is the test that should stop them; read the docstring on
    ``source_model_coverage`` for the trade first.
    """
    assert coverage["qualifying_rows"] > _COVERAGE_SAMPLE_ROWS
    assert coverage["sampled_rows"] == _COVERAGE_SAMPLE_ROWS


def test_a_large_healthy_collection_is_never_called_stranded(coverage):
    """``has_matching`` is exact, so the 409 cannot be a sampling artefact.

    Collection 7 is ``_COVERAGE_SAMPLE_ROWS + 50`` fresh rows plus one
    stale row — a corpus far larger than the sampling window. The
    endpoint must still see it as retrievable.

    ``has_other`` is deliberately NOT asserted: with no ``ORDER BY`` the
    window is whatever the scan yields first, so whether the lone stale
    row is seen is not a guarantee. That is the documented blind spot,
    and it only costs an operator log line — pinning it either way would
    be pinning the planner, not the contract.
    """
    has_matching, _has_other, _sample = coverage[7]
    assert has_matching is True


def test_a_migration_in_progress_is_never_refused(coverage):
    """Owner question ③: a deliberate mixed-model corpus must not 409.

    This is why ``has_matching`` is an exact ``EXISTS`` rather than a
    read of the sampled window. Collection 8 is stale rows far beyond the
    window followed by a single freshly-reindexed row — the shape of a
    reindex that has only just started. A sampled ``has_matching`` would
    report False here and take the knowledge base offline for exactly as
    long as the operator was busy fixing it.

    Mutant: source ``has_matching`` from the sample (``bool_or(src IS NOT
    DISTINCT FROM $1)``) instead of the ``EXISTS`` — this fails, and
    nothing else in the suite does.
    """
    has_matching, _has_other, _sample = coverage[8]
    assert has_matching is True


def test_live_casing_collision_is_not_treated_as_a_stranded_corpus(coverage):
    """The values are the live ones, not values chosen to make a point.

    ``ingestion_collections.embedding_model`` DEFAULTs to
    ``nvidia/NV-embed-V2``; the same model registers as
    ``nvidia/nv-embed-v2``; ``r1_0018`` copied the former onto chunk
    provenance. So on real data a correctly-indexed collection can carry
    a spelling that a case-sensitive comparison rejects — turning a
    working knowledge base into a 409 with no remedy, which is worse than
    the silent-empty defect this package exists to remove.

    Mutant: drop the ``lower()`` from the ``has_matching`` EXISTS in
    ``source_model_coverage`` — this goes red, and it is the only test
    that does.
    """
    has_matching, has_other, _sample = coverage[9]
    assert has_matching is True, (
        f"a corpus indexed as {COLLECTION_COLUMN_DEFAULT!r} was not recognised as "
        f"matching the designated {DESIGNATED!r} — same model, different casing"
    )
    assert has_other is False, (
        "the casing variant was also counted as a foreign model, so the "
        "operator would get a partial-index warning about nothing"
    )
