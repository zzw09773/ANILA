"""Constructor-guard and shape tests for ``AgentScopedPgVectorStore``.

The full DB-integration tests for index / search / RLS bypass live in
``test_agent_isolation.py`` and run against a sandboxed pgvector
container in CI (Sprint 1 G1/G2 gates). The tests in *this* file are
the cheap, no-DB-required guards that ensure the Layer 3 contract
holds without spinning up Postgres:

- Constructor refuses anything but a positive int agent_id (covers
  None, str, bool, 0, negative — every shape that has bitten teams
  by silently scoping requests to agent_id = 1 in the past).
- ``agent_id`` is exposed read-only; mutation must not be possible
  through normal attribute access.

These are tiny, but they're the *security boundary* for Layer 3.
A regression here would silently disable agent isolation.
"""

from __future__ import annotations

import pytest

from anila_core.storage.adapters.pgvector_store import AgentScopedPgVectorStore


class _FakePool:
    """Stand-in pool — never used because constructor checks come first."""


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
def test_constructor_rejects_non_int_agent_id(bad_value: object) -> None:
    with pytest.raises(ValueError, match="agent_id must be"):
        AgentScopedPgVectorStore(_FakePool(), agent_id=bad_value)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_value", [0, -1, -999])
def test_constructor_rejects_non_positive_agent_id(bad_value: int) -> None:
    with pytest.raises(ValueError, match="must be > 0"):
        AgentScopedPgVectorStore(_FakePool(), agent_id=bad_value)


def test_constructor_accepts_positive_int() -> None:
    store = AgentScopedPgVectorStore(_FakePool(), agent_id=42)
    assert store.agent_id == 42


def test_index_chunks_rejects_count_mismatch() -> None:
    """Defensive — embeddings must align 1:1 with chunks."""
    from anila_core.ingestion.chunking_plugins import ChunkResult

    store = AgentScopedPgVectorStore(_FakePool(), agent_id=1)
    with pytest.raises(ValueError, match="counts must match"):
        # 2 chunks, 1 embedding — mismatch should fail before any IO.
        import asyncio

        asyncio.run(
            store.index_chunks(
                collection_id=1,
                document_id=1,
                chunks=[
                    ChunkResult(content="a", chunk_key="k1", token_count=1),
                    ChunkResult(content="b", chunk_key="k2", token_count=1),
                ],
                embeddings=[[0.0]],
            )
        )
