from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ingestion_worker.similarity_relations import (
    SimilarityClaim,
    fail_similarity_recompute,
    finish_similarity_recompute,
    recompute_similarity_edges,
    request_similarity_recompute,
)


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


class _Connection:
    def __init__(self):
        self.fetchrow = AsyncMock()
        self.fetchval = AsyncMock()
        self.fetch = AsyncMock()
        self.execute = AsyncMock(return_value="UPDATE 1")

    def transaction(self):
        return _AsyncContext(self)


class _Pool:
    def __init__(self, conn: _Connection):
        self.conn = conn

    def acquire(self):
        return _AsyncContext(self.conn)


@pytest.mark.asyncio
async def test_request_is_one_row_per_collection_with_monotonic_upsert():
    conn = _Connection()
    conn.fetchrow.return_value = {"request_seq": 7}

    seq = await request_similarity_recompute(
        _Pool(conn), collection_id=42, debounce_seconds=12.5
    )

    assert seq == 7
    sql, collection_id, debounce = conn.fetchrow.await_args.args
    assert "ON CONFLICT (collection_id) DO UPDATE" in sql
    assert "request_seq=similarity_recompute_requests.request_seq+1" in sql
    assert collection_id == 42
    assert debounce == 12.5


@pytest.mark.asyncio
async def test_finish_preserves_exactly_one_followup_when_new_request_arrived():
    conn = _Connection()
    conn.fetchrow.return_value = {
        "request_seq": 3,
        "claimed_seq": 2,
        "not_before": None,
    }
    claim = SimilarityClaim(collection_id=9, request_seq=2, lease_token="lease")

    assert await finish_similarity_recompute(_Pool(conn), claim=claim)

    statements = [call.args[0] for call in conn.execute.await_args_list]
    assert any("SET status='pending'" in sql for sql in statements)
    assert not any("DELETE FROM similarity_recompute_requests" in sql for sql in statements)


@pytest.mark.asyncio
async def test_finish_deletes_fully_drained_request():
    conn = _Connection()
    conn.fetchrow.return_value = {
        "request_seq": 2,
        "claimed_seq": 2,
        "not_before": None,
    }
    claim = SimilarityClaim(collection_id=9, request_seq=2, lease_token="lease")

    assert await finish_similarity_recompute(_Pool(conn), claim=claim)

    statements = [call.args[0] for call in conn.execute.await_args_list]
    assert any("DELETE FROM similarity_recompute_requests" in sql for sql in statements)


@pytest.mark.asyncio
async def test_failed_owned_claim_returns_to_pending_without_payload_leak():
    conn = _Connection()
    claim = SimilarityClaim(collection_id=11, request_seq=1, lease_token="lease")

    assert await fail_similarity_recompute(
        _Pool(conn),
        claim=claim,
        error=RuntimeError("x" * 2000),
        backoff_seconds=30,
    )

    args = conn.execute.await_args.args
    assert "SET status='pending'" in args[0]
    assert len(args[4]) == 1000
    assert "collection" not in args[4].lower()


@pytest.mark.asyncio
async def test_over_cap_recompute_clears_stale_similarity_edges():
    conn = _Connection()
    conn.fetchrow.return_value = {"collection_id": 4}
    conn.fetchval.return_value = 501
    settings = SimpleNamespace(
        enable_similarity_edges=True,
        similarity_max_docs=500,
        similarity_min=0.75,
        similarity_top_k=3,
    )

    result = await recompute_similarity_edges(
        _Pool(conn),
        claim=SimilarityClaim(collection_id=4, request_seq=7, lease_token="lease"),
        collection_id=4,
        run_id="run",
        settings=settings,
    )

    assert result == {"edges": 0}
    assert "is_active_generation = true" in conn.fetchval.await_args.args[0]
    statements = [call.args[0] for call in conn.execute.await_args_list]
    assert any(
        "DELETE FROM document_relations" in sql and "source = 'similarity'" in sql
        for sql in statements
    )


@pytest.mark.asyncio
async def test_lost_claim_cannot_clear_or_replace_similarity_edges():
    conn = _Connection()
    conn.fetchrow.return_value = None
    settings = SimpleNamespace(
        enable_similarity_edges=True,
        similarity_max_docs=500,
        similarity_min=0.75,
        similarity_top_k=3,
    )

    from ingestion_worker.similarity_relations import SimilarityLeaseLostError

    with pytest.raises(SimilarityLeaseLostError):
        await recompute_similarity_edges(
            _Pool(conn),
            claim=SimilarityClaim(collection_id=4, request_seq=7, lease_token="stale"),
            collection_id=4,
            run_id="stale-run",
            settings=settings,
        )

    statements = [call.args[0] for call in conn.execute.await_args_list]
    assert not any("DELETE FROM document_relations" in sql for sql in statements)


def test_centroid_query_excludes_staging_and_retired_generations():
    from ingestion_worker import similarity_relations

    assert "is_active_generation = true" in similarity_relations._PAIRS_SQL
