"""Sprint 1 Gate G1 (Sprint 4 rebase) — random workload, zero leakage.

Per docs/ingestion-platform-design.md §3.3 Layer 4:

    Create 5 collections, each ingests N chunks. Run M random queries
    from each collection's perspective. Every chunk in every result
    must belong to the querying collection. A single leak = test
    failure = the refactored isolation guarantee broke.

Sprint 4 rebase: the unit-of-isolation moved from agents → collections.
Same algorithm, different scope key.
"""

from __future__ import annotations

import random

import pytest

from anila_core.ingestion.chunking_plugins import ChunkResult
from anila_core.storage.adapters.pg_pool import PgPool
from anila_core.storage.adapters.pgvector_store import CollectionScopedPgVectorStore


pytestmark = pytest.mark.asyncio


_CHUNKS_PER_COLLECTION = 50
_QUERIES_PER_COLLECTION = 30
_DIM = 4000


def _random_unit_vector(rng: random.Random, dim: int = _DIM) -> list[float]:
    raw = [rng.gauss(0, 1) for _ in range(dim)]
    norm = sum(x * x for x in raw) ** 0.5 or 1.0
    return [x / norm for x in raw]


async def test_g1_random_workload_no_cross_collection_leak(
    pool: PgPool,
    isolation_collections: list[int],
    isolation_documents: list[int],
) -> None:
    """5 × N chunk fixtures + N×5 random queries → zero leakage."""
    rng = random.Random(0xC1A1)

    # ── Insert phase ─────────────────────────────────────────────────────
    for coll_id, doc_id in zip(isolation_collections, isolation_documents):
        store = CollectionScopedPgVectorStore(pool, collection_id=coll_id)
        chunks: list[ChunkResult] = []
        embeddings: list[list[float]] = []
        for i in range(_CHUNKS_PER_COLLECTION):
            chunks.append(
                ChunkResult(
                    content=f"coll-{coll_id} chunk-{i}",
                    chunk_key=f"c{coll_id}-c{i}",
                    token_count=4,
                    metadata={"owner_collection": coll_id, "test": "g1"},
                )
            )
            embeddings.append(_random_unit_vector(rng))
        await store.index_chunks(
            document_id=doc_id, chunks=chunks, embeddings=embeddings,
        )

    # ── Query phase ─────────────────────────────────────────────────────
    leaks: list[tuple[int, int, str]] = []
    total = 0
    for querying_coll in isolation_collections:
        store = CollectionScopedPgVectorStore(pool, collection_id=querying_coll)
        for _ in range(_QUERIES_PER_COLLECTION):
            q = _random_unit_vector(rng)
            hits = await store.similarity_search(q, top_k=20)
            total += 1
            for hit in hits:
                if hit.chunk.collection_id != querying_coll:
                    leaks.append(
                        (querying_coll, hit.chunk.collection_id, hit.chunk.chunk_key)
                    )

    assert not leaks, (
        f"G1 LEAKAGE: {len(leaks)} cross-collection visibility events out of "
        f"{total} queries × 5 collections × {_CHUNKS_PER_COLLECTION} chunks. "
        f"First 3 leaks: {leaks[:3]}"
    )


async def test_g1_each_collection_sees_own_chunks(
    pool: PgPool,
    isolation_collections: list[int],
) -> None:
    """Sanity: every collection's own list_in_collection returns its rows."""
    for coll_id in isolation_collections:
        store = CollectionScopedPgVectorStore(pool, collection_id=coll_id)
        rows = await store.list_in_collection(limit=200)
        assert len(rows) == _CHUNKS_PER_COLLECTION, (
            f"Collection {coll_id} can only see {len(rows)} of its own "
            f"{_CHUNKS_PER_COLLECTION} chunks — RLS over-blocked or insert "
            f"failed silently."
        )
        for row in rows:
            assert row.collection_id == coll_id
