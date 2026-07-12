"""Constructor-guard and shape tests for ``CollectionScopedPgVectorStore``.

Sprint 4 rename — was ``test_agent_scoped_pgvector_store.py``. Behavior
is the same defensive contract, just keyed on ``collection_id`` instead
of ``agent_id``.

The full DB-integration tests live in ``test_g1_collection_isolation.py``
and run against a sandboxed pgvector container (Sprint 4 G1/G2 gates).
The tests in *this* file are the cheap, no-DB-required guards that
ensure the Layer 3 contract holds without spinning up Postgres:

- Constructor refuses anything but a positive int collection_id (covers
  None, str, bool, 0, negative — every shape that has bitten teams
  by silently scoping requests to collection_id = 1 in the past).
- ``collection_id`` is exposed read-only; mutation must not be possible
  through normal attribute access.

These are tiny, but they're the *security boundary* for Layer 3.
A regression here would silently disable collection isolation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from anila_contracts import Classification

from anila_core.ingestion.chunking_plugins import ChunkResult
from anila_core.ingestion.errors import StoreError
from anila_core.storage.adapters.pgvector_store import (
    AgentScopedPgVectorStore,
    CollectionScopedPgVectorStore,
)


class _FakePool:
    """Stand-in pool — never used because constructor checks come first."""


class _Transaction:
    async def start(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        return False


class _Acquire:
    def __init__(self, connection) -> None:
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        return False


class _RecordingConnection:
    def __init__(
        self,
        *,
        collection_level: str = "無機密",
        document_level: str = "無機密",
        document_collection_id: int = 9,
    ) -> None:
        self.collection_level = collection_level
        self.document_level = document_level
        self.document_collection_id = document_collection_id
        self.executemany_calls: list[tuple[str, list[tuple]]] = []
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.fetchrow_calls: list[tuple[str, tuple]] = []
        self.execute_calls: list[tuple[str, tuple]] = []

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def execute(self, _sql: str, *_args) -> str:
        self.execute_calls.append((_sql, _args))
        return "OK"

    async def executemany(self, sql: str, rows: list[tuple]) -> None:
        self.executemany_calls.append((sql, rows))

    async def fetch(self, sql: str, *args) -> list:
        self.fetch_calls.append((sql, args))
        return []

    async def fetchrow(self, sql: str, *args) -> dict:
        self.fetchrow_calls.append((sql, args))
        if "FROM ingestion_collections" in sql:
            return {"id": args[0], "classification_level": self.collection_level}
        if "FROM ingestion_documents" in sql:
            return {
                "id": args[0],
                "collection_id": self.document_collection_id,
                "classification_level": self.document_level,
            }
        return {"id": len(self.fetchrow_calls), "chunk_key": args[2]}


class _RecordingPool:
    def __init__(self, connection: _RecordingConnection) -> None:
        self.connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


@pytest.mark.parametrize(
    "bad_value",
    [
        None,
        "1",
        1.0,
        True,  # bool is a subclass of int in Python; must be rejected.
        False,
        [1],
        {"id": 1},
    ],
)
def test_constructor_rejects_non_int_collection_id(bad_value: object) -> None:
    with pytest.raises(ValueError, match="collection_id must be"):
        CollectionScopedPgVectorStore(_FakePool(), collection_id=bad_value)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_value", [0, -1, -999])
def test_constructor_rejects_non_positive_collection_id(bad_value: int) -> None:
    with pytest.raises(ValueError, match="must be > 0"):
        CollectionScopedPgVectorStore(_FakePool(), collection_id=bad_value)


def test_constructor_accepts_positive_int() -> None:
    store = CollectionScopedPgVectorStore(_FakePool(), collection_id=42)
    assert store.collection_id == 42


def test_back_compat_alias_resolves_to_new_class() -> None:
    """``AgentScopedPgVectorStore`` must alias the new class for one cycle.

    External callers still importing the old name pick up the new
    ``collection_id`` semantics; they fail at the call site with a
    different param name, which is the intended forcing function for
    them to update.
    """
    assert AgentScopedPgVectorStore is CollectionScopedPgVectorStore


def test_index_chunks_rejects_count_mismatch() -> None:
    """Defensive — embeddings must align 1:1 with chunks."""
    import asyncio

    from anila_core.ingestion.chunking_plugins import ChunkResult

    store = CollectionScopedPgVectorStore(_FakePool(), collection_id=1)
    with pytest.raises(ValueError, match="counts must match"):
        asyncio.run(
            store.index_chunks(
                document_id=1,
                chunks=[
                    ChunkResult(content="a", chunk_key="k1", token_count=1),
                    ChunkResult(content="b", chunk_key="k2", token_count=1),
                ],
                embeddings=[[0.0]],
                classification_level=Classification.UNCLASSIFIED,
            )
        )


async def test_parent_and_leaf_insert_sql_persists_classification_provenance() -> None:
    connection = _RecordingConnection(
        collection_level="機密",
        document_level="極機密",
    )
    store = CollectionScopedPgVectorStore(
        _RecordingPool(connection),  # type: ignore[arg-type]
        collection_id=9,
    )
    leaf = ChunkResult(content="leaf", chunk_key="leaf-1", token_count=1)
    parent = ChunkResult(
        content="heading",
        chunk_key="parent-1",
        token_count=1,
        metadata={"chunk_type": "heading"},
    )

    await store.index_chunks(
        document_id=12,
        chunks=[leaf],
        embeddings=[[0.1]],
        classification_level=Classification.UNCLASSIFIED,
    )
    await store.add_parent_chunks(
        document_id=12,
        chunks=[parent],
        classification_level=Classification.UNCLASSIFIED,
    )

    leaf_sql, leaf_rows = connection.executemany_calls[0]
    assert "classification_level" in leaf_sql
    assert "classification_latched_at" in leaf_sql
    assert "classification_source" in leaf_sql
    assert "CURRENT_TIMESTAMP" in leaf_sql
    assert leaf_rows[0][-2:] == ("極機密", "ingestion_effective")

    parent_sql, parent_args = next(
        call for call in connection.fetchrow_calls if "INSERT INTO document_chunks" in call[0]
    )
    assert "classification_level" in parent_sql
    assert "classification_latched_at" in parent_sql
    assert "classification_source" in parent_sql
    assert "CURRENT_TIMESTAMP" in parent_sql
    assert parent_args[-2:] == ("極機密", "ingestion_effective")

    lock_queries = [
        sql
        for sql, _args in connection.fetchrow_calls
        if "FROM ingestion_" in sql
    ]
    assert len(lock_queries) == 4
    assert "FROM ingestion_collections" in lock_queries[0]
    assert "FROM ingestion_documents" in lock_queries[1]
    assert "FROM ingestion_collections" in lock_queries[2]
    assert "FROM ingestion_documents" in lock_queries[3]
    assert all("FOR SHARE" in sql for sql in lock_queries)


async def test_atomic_replace_deletes_then_inserts_one_classified_generation() -> None:
    connection = _RecordingConnection(
        collection_level="機密",
        document_level="極機密",
    )
    store = CollectionScopedPgVectorStore(
        _RecordingPool(connection),  # type: ignore[arg-type]
        collection_id=9,
    )
    parent = ChunkResult(
        content="heading",
        chunk_key="parent-1",
        token_count=1,
        metadata={"chunk_type": "heading", "chunk_level": 1},
    )
    leaf = ChunkResult(
        content="leaf",
        chunk_key="leaf-1",
        token_count=2,
        metadata={"chunk_type": "leaf", "parent_chunk_key": "parent-1"},
    )

    written = await store.replace_document_chunks(
        document_id=12,
        parent_chunks=[parent],
        leaf_chunks=[leaf],
        embeddings=[[0.1]],
        classification_level=Classification.UNCLASSIFIED,
    )

    assert written == 2
    delete_index = next(
        index
        for index, (sql, args) in enumerate(connection.execute_calls)
        if "DELETE FROM document_chunks" in sql and args == (12,)
    )
    # SET LOCAL always precedes the destructive replacement.
    assert delete_index > 0
    parent_sql, parent_args = next(
        call
        for call in connection.fetchrow_calls
        if "INSERT INTO document_chunks" in call[0]
    )
    assert "RETURNING id, chunk_key" in parent_sql
    assert parent_args[-2:] == ("極機密", "ingestion_effective")
    leaf_sql, leaf_rows = connection.executemany_calls[-1]
    assert "parent_chunk_id" in leaf_sql
    assert leaf_rows[0][-2:] == ("極機密", "ingestion_effective")
    assert leaf_rows[0][-3] == 3


async def test_atomic_replace_rejects_invalid_generation_before_database_use() -> None:
    store = CollectionScopedPgVectorStore(_FakePool(), collection_id=9)
    parent = ChunkResult(content="p", chunk_key="same", token_count=1)
    duplicate = ChunkResult(content="l", chunk_key="same", token_count=1)
    with pytest.raises(ValueError, match="duplicate chunk_key"):
        await store.replace_document_chunks(
            document_id=12,
            parent_chunks=[parent],
            leaf_chunks=[duplicate],
            embeddings=[[0.1]],
            classification_level=Classification.UNCLASSIFIED,
        )

    orphan = ChunkResult(
        content="l",
        chunk_key="leaf",
        token_count=1,
        metadata={"parent_chunk_key": "missing"},
    )
    with pytest.raises(ValueError, match="unknown parent_chunk_key"):
        await store.replace_document_chunks(
            document_id=12,
            parent_chunks=[],
            leaf_chunks=[orphan],
            embeddings=[[0.1]],
            classification_level=Classification.UNCLASSIFIED,
        )

    with pytest.raises(ValueError, match="counts must match"):
        await store.replace_document_chunks(
            document_id=12,
            parent_chunks=[],
            leaf_chunks=[orphan],
            embeddings=[],
            classification_level=Classification.UNCLASSIFIED,
        )


async def test_counter_reconciliation_stays_inside_canonical_store() -> None:
    connection = _RecordingConnection()
    store = CollectionScopedPgVectorStore(
        _RecordingPool(connection),  # type: ignore[arg-type]
        collection_id=9,
    )

    await store.reconcile_collection_counters()

    assert any(
        "FROM ingestion_collections" in sql and "FOR UPDATE" in sql
        for sql, _args in connection.fetchrow_calls
    )
    assert any(
        "UPDATE ingestion_collections" in sql
        and "FROM document_chunks" in sql
        for sql, _args in connection.execute_calls
    )


async def test_clearance_allowlist_is_applied_in_sql_before_global_ranking() -> None:
    connection = _RecordingConnection()
    store = CollectionScopedPgVectorStore(
        _RecordingPool(connection),  # type: ignore[arg-type]
        collection_id=9,
    )

    assert await store.similarity_search_scoped_documents(
        query_embedding=[0.1],
        document_ids=[12, 13],
        top_k=4,
        min_score=0.25,
        classification_ceiling=Classification.CONFIDENTIAL,
    ) == []
    sql, args = connection.fetch_calls[-1]
    assert "document_id = ANY($2::bigint[])" in sql
    assert "classification_level = ANY($3::text[])" in sql
    assert "LIMIT $5" in sql
    assert args[1:] == (
        [12, 13],
        ["無機密", "營業秘密", "機密"],
        0.25,
        4,
    )

    before = len(connection.fetch_calls)
    assert await store.similarity_search_scoped_documents(
        query_embedding=[0.1],
        document_ids=[],
        top_k=4,
        classification_ceiling=Classification.CONFIDENTIAL,
    ) == []
    assert len(connection.fetch_calls) == before

    assert await store.similarity_search_per_document_authorized(
        query_embedding=[0.1],
        document_ids=[12],
        classification_ceiling=Classification.TRADE_SECRET,
        k=2,
        min_score=0.1,
    ) == []
    per_document_sql, per_document_args = connection.fetch_calls[-1]
    assert "classification_level = ANY($3::text[])" in per_document_sql
    assert per_document_args[1:] == (
        [12],
        ["無機密", "營業秘密"],
        0.1,
        2,
    )

    await store._attach_parent_content(  # noqa: SLF001
        connection,
        [SimpleNamespace(chunk=SimpleNamespace(parent_chunk_id=99))],
        classification_ceiling=Classification.TRADE_SECRET,
    )
    parent_sql, parent_args = connection.fetch_calls[-1]
    assert "classification_level = ANY($3::text[])" in parent_sql
    assert parent_args == ([99], 9, ["無機密", "營業秘密"])

async def test_write_floor_preserves_higher_caller_and_rejects_wrong_owner() -> None:
    high_connection = _RecordingConnection()
    high_store = CollectionScopedPgVectorStore(
        _RecordingPool(high_connection),  # type: ignore[arg-type]
        collection_id=9,
    )
    await high_store.index_chunks(
        document_id=12,
        chunks=[ChunkResult(content="leaf", chunk_key="high", token_count=1)],
        embeddings=[[0.1]],
        classification_level=Classification.TOP_SECRET,
    )
    assert high_connection.executemany_calls[0][1][0][-2:] == (
        "絕對機密",
        "ingestion_effective",
    )

    wrong_owner = _RecordingConnection(document_collection_id=10)
    wrong_store = CollectionScopedPgVectorStore(
        _RecordingPool(wrong_owner),  # type: ignore[arg-type]
        collection_id=9,
    )
    with pytest.raises(StoreError) as exc_info:
        await wrong_store.index_chunks(
            document_id=12,
            chunks=[ChunkResult(content="leaf", chunk_key="wrong", token_count=1)],
            embeddings=[[0.1]],
            classification_level=Classification.UNCLASSIFIED,
        )
    assert exc_info.value.code == "E_PG_RLS_VIOLATION"
    assert wrong_owner.executemany_calls == []


@pytest.mark.parametrize(
    ("collection_level", "document_level"),
    [("UNKNOWN", "無機密"), ("無機密", "UNKNOWN"), (None, "無機密")],
)
async def test_write_floor_rejects_invalid_locked_source_classification(
    collection_level: object,
    document_level: object,
) -> None:
    connection = _RecordingConnection(
        collection_level=collection_level,  # type: ignore[arg-type]
        document_level=document_level,  # type: ignore[arg-type]
    )
    store = CollectionScopedPgVectorStore(
        _RecordingPool(connection),  # type: ignore[arg-type]
        collection_id=9,
    )

    with pytest.raises(StoreError) as exc_info:
        await store.index_chunks(
            document_id=12,
            chunks=[ChunkResult(content="leaf", chunk_key="bad", token_count=1)],
            embeddings=[[0.1]],
            classification_level=Classification.UNCLASSIFIED,
        )

    assert exc_info.value.code == "E_CLASSIFICATION_INVALID"
    assert connection.executemany_calls == []


@pytest.mark.parametrize("bad_value", ["機密", None, 2])
async def test_write_paths_reject_noncanonical_classification(bad_value: object) -> None:
    store = CollectionScopedPgVectorStore(_FakePool(), collection_id=1)

    with pytest.raises(ValueError, match="anila_contracts.Classification"):
        await store.index_chunks(
            document_id=1,
            chunks=[],
            embeddings=[],
            classification_level=bad_value,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="anila_contracts.Classification"):
        await store.add_parent_chunks(
            document_id=1,
            chunks=[],
            classification_level=bad_value,  # type: ignore[arg-type]
        )


def _chunk_row(classification_level: object) -> dict:
    return {
        "id": 1,
        "collection_id": 2,
        "document_id": 3,
        "chunk_key": "chunk-1",
        "content": "content",
        "metadata": {},
        "token_count": 1,
        "created_at": datetime.now(timezone.utc),
        "classification_level": classification_level,
        "classification_latched_at": datetime.now(timezone.utc),
        "classification_source": "ingestion_effective",
        "score": 0.9,
    }


def test_row_mappers_carry_canonical_classification_and_provenance() -> None:
    row = _chunk_row("機密")

    chunk = CollectionScopedPgVectorStore._row_to_chunk(  # noqa: SLF001
        row,  # type: ignore[arg-type]
        include_embedding=False,
    )
    hit = CollectionScopedPgVectorStore._row_to_search_hit(row)  # type: ignore[arg-type]  # noqa: SLF001

    assert chunk.classification_level is Classification.CONFIDENTIAL
    assert chunk.classification_source == "ingestion_effective"
    assert chunk.classification_latched_at == row["classification_latched_at"]
    assert hit.chunk.classification_level is Classification.CONFIDENTIAL
    assert hit.chunk.classification_source == "ingestion_effective"


@pytest.mark.parametrize("bad_value", [None, "UNKNOWN", ""])
def test_row_mappers_fail_closed_on_invalid_classification(bad_value: object) -> None:
    row = _chunk_row(bad_value)

    with pytest.raises(ValueError):
        CollectionScopedPgVectorStore._row_to_chunk(  # noqa: SLF001
            row,  # type: ignore[arg-type]
            include_embedding=False,
        )
    with pytest.raises(ValueError):
        CollectionScopedPgVectorStore._row_to_search_hit(  # type: ignore[arg-type]  # noqa: SLF001
            row
        )


async def test_every_chunk_read_query_selects_classification_contract() -> None:
    connection = _RecordingConnection()
    store = CollectionScopedPgVectorStore(
        _RecordingPool(connection),  # type: ignore[arg-type]
        collection_id=9,
    )

    await store.similarity_search([0.1])
    await store.similarity_search_per_document([0.1], [12])
    await store.keyword_search("term")
    await store.list_by_document(12)
    await store.list_in_collection()

    assert len(connection.fetch_calls) == 5
    for sql, _args in connection.fetch_calls:
        assert "classification_level" in sql
        assert "classification_latched_at" in sql
        assert "classification_source" in sql
