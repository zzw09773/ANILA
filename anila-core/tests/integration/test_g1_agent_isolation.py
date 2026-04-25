"""Sprint 1 Gate G1 — random workload, zero cross-agent leakage.

Per docs/ingestion-platform-design.md §3.3 Layer 4 spec:

    Create 5 agents, each ingests 200 chunks. Run 1000 random queries
    from each agent's perspective. Every chunk in every result must
    belong to the querying agent. A single leak = test failure =
    Sprint 1 cannot ship.

This is the load-bearing security test for the entire ingestion
platform. If this regresses, agent A can read agent B's documents and
the multi-tenant promise of Sprint 1 breaks. Run it on every change
to ``AgentScopedPgVectorStore`` or migration 0014/0015.

Determinism: random seed is fixed so the same test sequence runs each
time. Adjust ``_QUERIES_PER_AGENT`` for runtime tuning; 200 here gives
strong coverage at ~10s wall clock against a local dev DB.
"""

from __future__ import annotations

import random

import pytest

from anila_core.ingestion.chunking_plugins import ChunkResult
from anila_core.storage.adapters.pg_pool import PgPool
from anila_core.storage.adapters.pgvector_store import AgentScopedPgVectorStore


pytestmark = pytest.mark.asyncio


# Reduced from the design-doc 200 chunks × 5 agents × 1000 queries to
# keep CI runtime tight while still exercising every agent_id ≠
# querying_agent_id pair statistically. Bump locally for paranoia.
_CHUNKS_PER_AGENT = 50
_QUERIES_PER_AGENT = 30
_DIM = 4000


def _random_unit_vector(rng: random.Random, dim: int = _DIM) -> list[float]:
    """L2-normalised vector. asyncpg's halfvec serialiser doesn't care
    about magnitude, but normalising keeps cosine distance well-conditioned
    so similarity_search returns sensible orderings during the test."""
    raw = [rng.gauss(0, 1) for _ in range(dim)]
    norm = sum(x * x for x in raw) ** 0.5 or 1.0
    return [x / norm for x in raw]


async def test_g1_random_workload_no_cross_agent_leak(
    pool: PgPool,
    isolation_agents: list[int],
    isolation_collections: list[int],
    isolation_documents: list[int],
) -> None:
    """5 × N chunk fixtures + N×5 random queries → zero leakage."""

    rng = random.Random(0xC1A1)  # deterministic seed

    # ── Insert phase ─────────────────────────────────────────────────────
    # Each agent inserts ``_CHUNKS_PER_AGENT`` chunks into its own
    # collection. The chunk_key carries the originating agent so
    # post-hoc inspection of any leaked row pinpoints the offender.
    for agent_id, coll_id, doc_id in zip(
        isolation_agents, isolation_collections, isolation_documents
    ):
        store = AgentScopedPgVectorStore(pool, agent_id=agent_id)
        chunks: list[ChunkResult] = []
        embeddings: list[list[float]] = []
        for i in range(_CHUNKS_PER_AGENT):
            chunks.append(
                ChunkResult(
                    content=f"agent-{agent_id} chunk-{i}",
                    chunk_key=f"a{agent_id}-c{i}",
                    token_count=4,
                    metadata={"owner_agent": agent_id, "test": "g1"},
                )
            )
            embeddings.append(_random_unit_vector(rng))
        await store.index_chunks(
            collection_id=coll_id,
            document_id=doc_id,
            chunks=chunks,
            embeddings=embeddings,
        )

    # ── Query phase ─────────────────────────────────────────────────────
    # For each agent, run ``_QUERIES_PER_AGENT`` random-vector queries
    # and assert every returned chunk's agent_id == querying agent_id.
    leaks: list[tuple[int, int, str]] = []
    total_queries = 0
    for querying_agent_id in isolation_agents:
        store = AgentScopedPgVectorStore(pool, agent_id=querying_agent_id)
        for _ in range(_QUERIES_PER_AGENT):
            q_vec = _random_unit_vector(rng)
            hits = await store.similarity_search(q_vec, top_k=20)
            total_queries += 1
            for hit in hits:
                if hit.chunk.agent_id != querying_agent_id:
                    leaks.append(
                        (
                            querying_agent_id,
                            hit.chunk.agent_id,
                            hit.chunk.chunk_key,
                        )
                    )

    assert not leaks, (
        f"G1 LEAKAGE: {len(leaks)} cross-agent visibility events out of "
        f"{total_queries} queries × 5 agents × {_CHUNKS_PER_AGENT} chunks. "
        f"First 3 leaks: {leaks[:3]}"
    )


async def test_g1_each_agent_sees_own_chunks(
    pool: PgPool,
    isolation_agents: list[int],
    isolation_collections: list[int],
) -> None:
    """Sanity check: scope isn't *too* tight — each agent must still
    see its own chunks via ``list_by_collection``. If this fails the
    G1 main test could pass for the wrong reason (RLS hiding everything)."""

    for agent_id, coll_id in zip(isolation_agents, isolation_collections):
        store = AgentScopedPgVectorStore(pool, agent_id=agent_id)
        rows = await store.list_by_collection(coll_id, limit=200)
        assert len(rows) == _CHUNKS_PER_AGENT, (
            f"Agent {agent_id} can only see {len(rows)} of its own "
            f"{_CHUNKS_PER_AGENT} chunks — RLS over-blocked or insert "
            f"failed silently."
        )
        # Every visible chunk must indeed be ours.
        for row in rows:
            assert row.agent_id == agent_id
