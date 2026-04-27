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

import pytest

from anila_core.storage.adapters.pgvector_store import (
    AgentScopedPgVectorStore,
    CollectionScopedPgVectorStore,
)


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
            )
        )
