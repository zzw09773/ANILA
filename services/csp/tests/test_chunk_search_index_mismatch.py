"""Search must not answer "nothing" when the index is under another model.

The defect this file guards: ``similarity_search`` filters chunks on the
embedding model that produced them (P4.8). Designating a different
platform embedding is a supported operator action, and the instant it
happens every existing chunk stops matching — the query still succeeds,
the table is still full, and the endpoint returns
``200 {"results": []}`` forever with no error and no log line. A
knowledge base full of documents becomes one that answers nothing, and
nothing anywhere says why.

The invariant under test, stated as the two cases a caller must be able
to tell apart:

  1. "this corpus has no matching passages"  → unchanged: ``200`` with
     ``results: []``. Legitimately-empty behaviour must not move.
  2. "this corpus is indexed under a different model than the one now
     designated" → ``409``, plus an operator log naming the stale model.

The production line that decides this is the
``_assert_index_matches_designation(...)`` call in
``app/api/ingestion/search.py`` immediately after ``similarity_search``.
Delete that one line and the endpoint reverts to silent-empty; the
mismatch tests below go red. None of them read source text, so a
grep-shaped mutation cannot fool them.

⚠ SQLite vs PostgreSQL. The pgvector layer cannot run under the SQLite
test fixture (asyncpg + halfvec), so ``CollectionScopedPgVectorStore`` is
stubbed here exactly as every other test in ``test_chunk_search_*`` does.
What that means precisely:

  * VERIFIED here: the endpoint's decision — which of the three coverage
    shapes produces 409, which produces a plain empty 200, and that the
    document_ids post-filter can never be misread as a stranded index.
  * NOT verified here: that
    ``CollectionScopedPgVectorStore.source_model_coverage``'s SQL returns
    those shapes on PostgreSQL. It relies on ``IS DISTINCT FROM``
    (Postgres/standard SQL; SQLite has ``IS`` with the same NULL-safe
    semantics but the tests never execute it) and on the RLS GUC set by
    ``_acquire()``. That half needs a live-Postgres check before this is
    called proven end to end.
"""
from __future__ import annotations

import logging
import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient

import app.api.ingestion.search as search_mod
from anila_core.storage.adapters.pgvector_store import SourceModelCoverage
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.models.model_registry import ModelRegistry
from app.services.auth_service import create_tokens

from tests.conftest import make_user


DESIGNATED = "nvidia/nv-embed-v2"
STALE = "legacy/old-embedder"

# Read out of the live database on 2026-08-05, not chosen for the test:
#   ingestion_collections.embedding_model DEFAULT = 'nvidia/NV-embed-V2'
#   model_registry.name                           = 'nvidia/nv-embed-v2'
# Migration r1_0018 copied the former onto chunk provenance.
LIVE_COLLECTION_DEFAULT = "nvidia/NV-embed-V2"
LIVE_REGISTRY_NAME = "nvidia/nv-embed-v2"


def _bearer(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_tokens(user)['access_token']}"}


@pytest.fixture
def alice(db):
    return make_user(db, username="mismatch-alice", role="user")


@pytest.fixture
def designated_model(db) -> ModelRegistry:
    """A designated platform embedding — without one there is no source
    filter, and therefore no mismatch to detect."""
    row = ModelRegistry(
        name=DESIGNATED,
        display_name=DESIGNATED,
        model_type="embedding",
        endpoint_url="http://embed.test/v1",
        is_active=True,
        is_platform_embedding=True,
        embedding_native_dim=4096,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def collection(db, alice) -> IngestionCollection:
    coll = IngestionCollection(
        name="mismatch-kb",
        chunking_config={"strategy": "semantic"},
        embedding_model=DESIGNATED,
        embedding_dim=4096,
        status="active",
        created_by=alice.id,
    )
    db.add(coll)
    db.commit()
    db.refresh(coll)

    doc = IngestionDocument(
        collection_id=coll.id,
        filename="regulation.pdf",
        sha256="b" * 64,
        mime_type="application/pdf",
        status="indexed",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    coll._test_doc_id = doc.id
    return coll


# ── stubs ────────────────────────────────────────────────────────────────────


class _StubChunk:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class _StubHit:
    def __init__(self, *, chunk, score, parent_content=None):
        self.chunk = chunk
        self.score = score
        self.parent_content = parent_content


class _StubStore:
    """Stands in for CollectionScopedPgVectorStore.

    ``coverage`` is what the real ``source_model_coverage`` would report
    for this collection; ``source_model_calls`` records that the endpoint
    only asks on the zero-hit path.
    """

    def __init__(self, hits, coverage: SourceModelCoverage):
        self._hits = hits
        self._coverage = coverage
        self.source_model_calls: list[str] = []

    async def similarity_search(self, *, query_embedding, top_k, min_score, **_kwargs):
        return self._hits

    async def source_model_coverage(self, source_model: str) -> SourceModelCoverage:
        self.source_model_calls.append(source_model)
        return self._coverage


def _install_store(monkeypatch, store: _StubStore) -> None:
    monkeypatch.setattr(
        search_mod,
        "CollectionScopedPgVectorStore",
        lambda pool, collection_id: store,
    )
    monkeypatch.setattr(search_mod, "get_pool", lambda: object())


def _hit(doc_id: int, chunk_id: int = 1001):
    return _StubHit(
        chunk=_StubChunk(
            id=chunk_id,
            document_id=doc_id,
            chunk_key=f"chunk:{doc_id}:{chunk_id}",
            content="第三條 承辦單位應於七日內完成審查。",
            metadata={},
            parent_chunk_id=None,
            chunk_type="leaf",
            chunk_level=0,
        ),
        score=0.42,
    )


@pytest.fixture(autouse=True)
def _stub_embed(monkeypatch):
    async def fake_embed_query(db, user, model_name, dim, query):
        return [0.1] * dim

    monkeypatch.setattr(search_mod, "_embed_query", fake_embed_query)


# ── case 2: the whole corpus is indexed under another model → 409 ────────────


def test_search_409s_when_every_chunk_is_under_another_model(
    client: TestClient, db, alice, designated_model, collection, monkeypatch,
):
    """The defect, exactly: chunks exist, none under the designated model.

    Mutant: delete the ``_assert_index_matches_designation`` call in
    ``search_collection`` — this reverts to 200 with ``results: []`` and
    the test fails on the status code.
    """
    store = _StubStore(
        [],
        SourceModelCoverage(
            has_matching=False, has_other=True, sample_other_model=STALE,
        ),
    )
    _install_store(monkeypatch, store)

    resp = client.post(
        f"/api/ingestion/collections/{collection.id}/search",
        json={"query": "審查期限是幾天"},
        headers=_bearer(alice),
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    # The caller must be able to tell this apart from "no data" by reading
    # the message, not by inferring it.
    assert "不是沒有資料" in detail
    assert DESIGNATED in detail
    assert store.source_model_calls == [DESIGNATED]


def test_mismatch_409_tells_the_user_how_to_get_service_back(
    client: TestClient, db, alice, designated_model, collection, monkeypatch,
):
    """A diagnosis is not a remedy — and a remedy that does not work is worse.

    The first version of this message named two escape routes, neither of
    which functions: "re-upload the document" is swallowed by
    ``uq_documents_collection_sha256`` (the IntegrityError handler returns
    the existing row above the enqueue call, so no job is ever created),
    and "an administrator reindexes" names a capability this platform does
    not have. A user would have spent an afternoon on either.

    What is asserted here is only what was walked end to end:
      * the operator remedy that works — put the previous model back;
      * the user remedy that works — DELETE then upload, with its cost;
      * an explicit warning that plain re-upload does nothing, because
        that is the obvious thing to try and it silently fails.

    Mutant: strip the remedy sentences back to the diagnosis — this fails.
    """
    store = _StubStore(
        [],
        SourceModelCoverage(
            has_matching=False, has_other=True, sample_other_model=STALE,
        ),
    )
    _install_store(monkeypatch, store)

    resp = client.post(
        f"/api/ingestion/collections/{collection.id}/search",
        json={"query": "審查期限是幾天"},
        headers=_bearer(alice),
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    # Who can fix it, and the specific action — not just "ask an admin".
    assert "管理員" in detail
    assert "重新指定" in detail
    # Deactivating the designated model reaches this same 409 via the soft
    # fallback, and re-designating an inactive model is REFUSED (400
    # 已停用的模型不能設為平台主 embedding). A remedy that omits the
    # reactivation step points the operator at a blocked action.
    assert "重新啟用" in detail
    # The user's own route, and its cost stated rather than hidden.
    assert "先刪除" in detail
    assert "重新上傳" in detail
    assert "不會回來" in detail
    # The trap, named. Plain re-upload is the obvious move and does nothing.
    assert "直接重傳" in detail
    # And nothing that does not exist.
    assert "重新索引" not in detail, "the platform has no reindex capability to point at"


def test_mismatch_409_does_not_leak_infrastructure_detail(
    client: TestClient, db, alice, designated_model, collection, monkeypatch,
):
    """The stale model name is inventory detail for the operator log; it
    has no business in a response an end user reads."""
    store = _StubStore(
        [],
        SourceModelCoverage(
            has_matching=False, has_other=True, sample_other_model=STALE,
        ),
    )
    _install_store(monkeypatch, store)

    resp = client.post(
        f"/api/ingestion/collections/{collection.id}/search",
        json={"query": "任何問題"},
        headers=_bearer(alice),
    )
    assert resp.status_code == 409
    body = resp.text
    assert STALE not in body
    assert "embed.test" not in body
    assert "document_chunks" not in body


def test_mismatch_names_the_stale_model_in_the_operator_log(
    client: TestClient, db, alice, designated_model, collection, monkeypatch, caplog,
):
    """An operator who greps the log must find the model to reindex from."""
    store = _StubStore(
        [],
        SourceModelCoverage(
            has_matching=False, has_other=True, sample_other_model=STALE,
        ),
    )
    _install_store(monkeypatch, store)

    with caplog.at_level(logging.ERROR, logger=search_mod.__name__):
        resp = client.post(
            f"/api/ingestion/collections/{collection.id}/search",
            json={"query": "任何問題"},
            headers=_bearer(alice),
        )
    assert resp.status_code == 409
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert STALE in logged
    assert DESIGNATED in logged


# ── case 1: legitimately empty → unchanged 200 ───────────────────────────────


def test_empty_corpus_still_returns_a_plain_empty_200(
    client: TestClient, db, alice, designated_model, collection, monkeypatch,
):
    """Nothing indexed at all is not a mismatch — the old contract holds."""
    store = _StubStore(
        [],
        SourceModelCoverage(
            has_matching=False, has_other=False, sample_other_model=None,
        ),
    )
    _install_store(monkeypatch, store)

    resp = client.post(
        f"/api/ingestion/collections/{collection.id}/search",
        json={"query": "任何問題"},
        headers=_bearer(alice),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"] == []
    assert body["embedding_model"] == DESIGNATED
    assert body["embedding_dim"] == 4096


def test_correctly_indexed_corpus_with_no_match_returns_empty_200(
    client: TestClient, db, alice, designated_model, collection, monkeypatch,
):
    """Chunks under the designated model, query matched none of them.

    This is the case a heavy-handed guard would wrongly turn into a 409,
    so it is pinned: a query that genuinely finds nothing must keep
    finding nothing, quietly.
    """
    store = _StubStore(
        [],
        SourceModelCoverage(
            has_matching=True, has_other=False, sample_other_model=None,
        ),
    )
    _install_store(monkeypatch, store)

    resp = client.post(
        f"/api/ingestion/collections/{collection.id}/search",
        json={"query": "完全無關的問題"},
        headers=_bearer(alice),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"] == []


def test_a_migration_in_progress_is_never_refused(
    client: TestClient, db, alice, designated_model, collection, monkeypatch,
):
    """Owner question ③, first shape: a deliberate mixed-model corpus.

    Reindexing a large knowledge base is not atomic — for the duration,
    the collection holds vectors under both the old and the new model.
    That is legitimate work in progress, and a check that refused it
    would take the knowledge base offline for exactly as long as the
    operator was busy fixing it.

    ``has_matching`` is an exact ``EXISTS`` rather than a sampled answer
    precisely so this can be a guarantee and not a probability: one
    freshly-written row anywhere in the collection is enough to keep the
    endpoint answering.
    """
    store = _StubStore(
        [],
        SourceModelCoverage(
            has_matching=True, has_other=True, sample_other_model=STALE,
        ),
    )
    _install_store(monkeypatch, store)

    resp = client.post(
        f"/api/ingestion/collections/{collection.id}/search",
        json={"query": "任何問題"},
        headers=_bearer(alice),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"] == []


def test_partially_reindexed_corpus_answers_normally_and_warns_operator(
    client: TestClient, db, alice, designated_model, collection, monkeypatch, caplog,
):
    """Mid-reindex: search works over the matching half, so users are not
    blocked — but the operator is told the other half is unreachable."""
    store = _StubStore(
        [],
        SourceModelCoverage(
            has_matching=True, has_other=True, sample_other_model=STALE,
        ),
    )
    _install_store(monkeypatch, store)

    with caplog.at_level(logging.WARNING, logger=search_mod.__name__):
        resp = client.post(
            f"/api/ingestion/collections/{collection.id}/search",
            json={"query": "任何問題"},
            headers=_bearer(alice),
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"] == []
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert STALE in logged


# ── the guard must not fire on paths that are not a mismatch ─────────────────


def test_no_designation_means_no_probe_and_no_change(
    client: TestClient, db, alice, collection, monkeypatch,
):
    """No embedding model registered → no source filter → nothing to check.

    Deployments that never designated a platform embedding must not pay a
    round-trip, nor be able to 409.
    """
    store = _StubStore(
        [],
        SourceModelCoverage(
            has_matching=False, has_other=True, sample_other_model=STALE,
        ),
    )
    _install_store(monkeypatch, store)

    resp = client.post(
        f"/api/ingestion/collections/{collection.id}/search",
        json={"query": "任何問題"},
        headers=_bearer(alice),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"] == []
    assert store.source_model_calls == []


def test_hits_found_never_triggers_the_probe(
    client: TestClient, db, alice, designated_model, collection, monkeypatch,
):
    """The happy path must not pay for the diagnosis."""
    store = _StubStore(
        [_hit(collection._test_doc_id)],
        SourceModelCoverage(
            has_matching=False, has_other=True, sample_other_model=STALE,
        ),
    )
    _install_store(monkeypatch, store)

    resp = client.post(
        f"/api/ingestion/collections/{collection.id}/search",
        json={"query": "審查期限"},
        headers=_bearer(alice),
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["results"]) == 1
    assert store.source_model_calls == []


# ── the store method the endpoint leans on ───────────────────────────────────


class _RecordingConn:
    """Records statements; answers the coverage query with a canned row."""

    def __init__(self, row: dict | None):
        self._row = row
        self.executed: list[str] = []
        self.fetched: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args):
        self.executed.append(sql)
        return "SET"

    async def fetchrow(self, sql: str, *args):
        self.fetched.append((sql, args))
        return self._row


class _RecordingTransaction:
    def __init__(self):
        self.started = False
        self.committed = False

    async def start(self):
        self.started = True

    async def commit(self):
        self.committed = True

    async def rollback(self):  # pragma: no cover - failure path unused here
        pass


class _RecordingPool:
    def __init__(self, row: dict | None):
        self.conn = _RecordingConn(row)
        self.tr = _RecordingTransaction()
        self.conn.transaction = lambda: self.tr

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self_inner):
                return pool.conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_source_model_coverage_maps_the_three_shapes(monkeypatch):
    """Row → dataclass, including the NULL sample (legacy rows carry a
    NULL ``embedding_source_model``, and NULL must not be mistaken for
    "no other rows")."""
    from anila_core.storage.adapters.pgvector_store import (
        CollectionScopedPgVectorStore,
    )

    pool = _RecordingPool(
        {"has_matching": False, "has_other": True, "sample_other_model": None},
    )
    store = CollectionScopedPgVectorStore(pool, collection_id=12)

    coverage = await store.source_model_coverage(DESIGNATED)

    assert coverage.has_matching is False
    assert coverage.has_other is True
    assert coverage.sample_other_model is None

    # Exactly one query, with the designated name as its only parameter —
    # the name must be bound, never interpolated into the SQL.
    assert len(pool.conn.fetched) == 1
    sql, args = pool.conn.fetched[0]
    assert args == (DESIGNATED,)
    assert DESIGNATED not in sql


@pytest.mark.asyncio
async def test_source_model_coverage_runs_under_the_collection_rls_scope():
    """Layer 3: the GUC the RLS policy reads must be set on the same
    connection, before the query. Without it the policy returns zero rows
    and every collection would look genuinely empty — the exact confusion
    this method exists to remove."""
    from anila_core.storage.adapters.pgvector_store import (
        CollectionScopedPgVectorStore,
    )

    pool = _RecordingPool(
        {"has_matching": True, "has_other": False, "sample_other_model": None},
    )
    store = CollectionScopedPgVectorStore(pool, collection_id=12)

    await store.source_model_coverage(DESIGNATED)

    assert pool.tr.started and pool.tr.committed
    assert any(
        "SET LOCAL anila.collection_id = 12" in stmt for stmt in pool.conn.executed
    ), pool.conn.executed


@pytest.mark.asyncio
async def test_source_model_coverage_survives_an_empty_result():
    """No row back at all is "nothing indexed", not a crash."""
    from anila_core.storage.adapters.pgvector_store import (
        CollectionScopedPgVectorStore,
    )

    store = CollectionScopedPgVectorStore(_RecordingPool(None), collection_id=12)
    coverage = await store.source_model_coverage(DESIGNATED)

    assert coverage.has_matching is False
    assert coverage.has_other is False
    assert coverage.sample_other_model is None


def test_document_ids_filter_emptying_the_hits_is_not_a_mismatch(
    client: TestClient, db, alice, designated_model, collection, monkeypatch,
):
    """A caller narrowing to one document gets an empty list, not a 409.

    The vector layer DID return rows; the caller's own filter removed
    them. Diagnosing that as a stranded index would turn a normal
    "in this document" query into an error.
    """
    store = _StubStore(
        [_hit(collection._test_doc_id)],
        SourceModelCoverage(
            has_matching=False, has_other=True, sample_other_model=STALE,
        ),
    )
    _install_store(monkeypatch, store)

    resp = client.post(
        f"/api/ingestion/collections/{collection.id}/search",
        json={"query": "審查期限", "document_ids": [999999]},
        headers=_bearer(alice),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"] == []
    assert store.source_model_calls == []


# ── the model-name comparison, guarded in a DEFAULT run ─────────────────────
#
# The three ``lower()`` calls on ``embedding_source_model`` are the most
# load-bearing characters in this package: without them a correctly-indexed
# corpus returns 200 with zero results, silently — the exact defect the rest
# of this file exists to prevent. Two of them live in SQL that cannot run
# under the SQLite fixture (asyncpg + halfvec + HNSW), and the PostgreSQL
# lane that does execute them skips silently unless a DSN env var is set.
# A test nobody runs is not a test.
#
# So these tests take the SQL the store actually emits, lift out the
# ``embedding_source_model`` predicate, and evaluate that predicate in
# SQLite against the two spellings that exist in the live database. It is
# weaker than executing the whole query — it proves the comparison's
# semantics, not the plan — but it runs every time, with nothing set, and
# it goes red the moment a ``lower()`` is dropped. Honest and always-on
# beats strong and never-run.


def _predicate_for_source_model(sql: str) -> str:
    """Lift the ``embedding_source_model`` comparison out of emitted SQL."""
    lines = [
        ln.strip() for ln in sql.splitlines()
        if "embedding_source_model" in ln and ("=" in ln or "DISTINCT" in ln)
    ]
    assert len(lines) == 1, (
        "expected exactly one source-model comparison in this query; found "
        f"{len(lines)}. The SQL was reshaped — update this test rather than "
        f"deleting it.\n{sql}"
    )
    pred = lines[0]
    for prefix in ("AND ", "WHERE "):
        if pred.startswith(prefix):
            pred = pred[len(prefix) :]
    return pred.rstrip(",")


def _sqlite_says_match(predicate: str, stored: str, filtered_on: str) -> bool:
    """Evaluate the lifted predicate with real values, using SQLite as oracle.

    ``lower()`` is ASCII-identical in SQLite and PostgreSQL, and model names
    are ASCII, so the two engines agree on exactly the property under test.
    """
    import re
    import sqlite3

    expr = re.sub(r"\$\d+", "?", predicate)
    conn = sqlite3.connect(":memory:")
    try:
        row = conn.execute(
            f"SELECT ({expr}) FROM (SELECT ? AS embedding_source_model, ? AS src)",
            (filtered_on, stored, stored),
        ).fetchone()
    finally:
        conn.close()
    return bool(row[0])


class _SqlCapturingConn:
    def __init__(self):
        self.queries: list[str] = []

    def transaction(self):
        conn = self

        class _Tr:
            async def start(self_inner):
                return None

            async def commit(self_inner):
                return None

            async def rollback(self_inner):  # pragma: no cover
                return None

        return _Tr()

    async def execute(self, sql, *args):
        return "SET"

    async def fetch(self, sql, *args):
        self.queries.append(sql)
        return []

    async def fetchrow(self, sql, *args):
        self.queries.append(sql)
        return None


class _SqlCapturingPool:
    def __init__(self):
        self.conn = _SqlCapturingConn()

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_retrieval_filter_matches_across_the_live_casing_variants():
    """``similarity_search``'s provenance filter must be case-insensitive.

    Live values: ``ingestion_collections.embedding_model`` DEFAULTs to
    ``nvidia/NV-embed-V2``, migration r1_0018 copied that onto chunk
    provenance, and the same model registers as ``nvidia/nv-embed-v2``.
    A case-sensitive ``=`` hides every chunk of a healthy corpus.

    Mutant: drop ``lower()`` from the ``embedding_source_model`` filter in
    ``similarity_search`` — this fails, in a default run, with nothing set.
    """
    from anila_core.storage.adapters.pgvector_store import (
        CollectionScopedPgVectorStore,
    )

    pool = _SqlCapturingPool()
    store = CollectionScopedPgVectorStore(pool, collection_id=3)
    await store.similarity_search(
        query_embedding=[0.1] * 8, top_k=5, source_model=LIVE_REGISTRY_NAME,
    )

    assert pool.conn.queries, "similarity_search issued no query to capture"
    predicate = _predicate_for_source_model(pool.conn.queries[0])
    assert _sqlite_says_match(
        predicate, stored=LIVE_COLLECTION_DEFAULT, filtered_on=LIVE_REGISTRY_NAME
    ), (
        f"predicate {predicate!r} does not match a chunk stored as "
        f"{LIVE_COLLECTION_DEFAULT!r} against a designation of "
        f"{LIVE_REGISTRY_NAME!r} — same model, different casing. A healthy "
        f"corpus would return 200 with zero results, silently."
    )


@pytest.mark.asyncio
async def test_coverage_probe_matches_across_the_live_casing_variants():
    """Same guarantee for the exact ``EXISTS`` that decides the 409.

    Mutant: drop ``lower()`` from the ``has_matching`` EXISTS — this fails
    in a default run, without the PostgreSQL lane.
    """
    from anila_core.storage.adapters.pgvector_store import (
        CollectionScopedPgVectorStore,
    )

    pool = _SqlCapturingPool()
    store = CollectionScopedPgVectorStore(pool, collection_id=3)
    await store.source_model_coverage(LIVE_REGISTRY_NAME)

    assert pool.conn.queries, "source_model_coverage issued no query to capture"
    predicate = _predicate_for_source_model(pool.conn.queries[0])
    assert _sqlite_says_match(
        predicate, stored=LIVE_COLLECTION_DEFAULT, filtered_on=LIVE_REGISTRY_NAME
    ), (
        f"predicate {predicate!r} would report a healthy corpus as having no "
        f"matching chunks, which turns into a 409 on a working knowledge base."
    )
