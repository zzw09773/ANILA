"""Deterministic unit tests for the pure / side-effect-free helpers in
``ingestion_worker.evaluator``.

Scope:
- ``_cosine``: pure vector math.
- ``_score_strategy``: retrieval-metric aggregation (Hit@1 / Hit@5 / MRR)
  plus ``judge_avg`` bookkeeping over in-memory lists/dicts. Its only
  external dependency is ``embedder.embed`` (an HTTP call), which we
  replace with a deterministic in-memory fake so the metric logic can be
  exercised without any network / DB / redis.

Deliberately NOT tested here (see task notes): the async ``evaluate_strategies``
orchestration (needs PgPool + redis + real Embedder + on-disk blobs), and
``_load_sample_docs`` / ``_chunk_doc`` which require a live pool / chunker
embedder pre-pass.
"""
from __future__ import annotations

import math

from anila_core.ingestion.chunking_plugins import ChunkResult

from ingestion_worker.evaluator import (
    _cosine,
    _require_eval_data_clearance,
    _score_strategy,
)
from ingestion_worker.judge import JudgeCredential


# ── Test doubles ────────────────────────────────────────────────────────


class _FakeEmbedder:
    """Returns precomputed embeddings keyed by the exact input text.

    ``_score_strategy`` calls ``embed`` exactly once (for the batch of
    query texts), so we map each query text to its intended vector. This
    keeps the cosine ranking fully deterministic.
    """

    def __init__(self, table: dict[str, list[float]]):
        self._table = table
        self.calls: list[list[str]] = []

    async def embed(self, texts, *, user_id=None):
        self.calls.append(list(texts))
        return [self._table[t] for t in texts]


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *args):
        return False


class _ClearanceConnection:
    def __init__(self, allowed):
        self.allowed = allowed
        self.calls = []

    async def fetchval(self, query, *args):
        self.calls.append((query, args))
        return self.allowed


class _ClearancePool:
    def __init__(self, allowed):
        self.connection = _ClearanceConnection(allowed)

    def acquire(self):
        return _Acquire(self.connection)


def _chunk(content: str, token_count: int = 10) -> ChunkResult:
    return ChunkResult(
        content=content,
        chunk_key=f"key/{content}",
        token_count=token_count,
        metadata={},
    )


# ── _cosine (pure) ──────────────────────────────────────────────────────


def test_cosine_identical_unit_vectors_is_one():
    assert _cosine([1.0, 0.0], [1.0, 0.0]) == 1.0


def test_cosine_orthogonal_vectors_is_zero():
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_cosine_opposite_vectors_is_negative_one():
    assert _cosine([1.0, 0.0], [-1.0, 0.0]) == -1.0


def test_cosine_is_scale_invariant():
    # Cosine ignores magnitude — only direction matters.
    assert _cosine([3.0, 4.0], [6.0, 8.0]) == 1.0


def test_cosine_known_angle():
    # [1,1] vs [1,0] -> cos 45deg = 1/sqrt(2).
    assert math.isclose(_cosine([1.0, 1.0], [1.0, 0.0]), 1 / math.sqrt(2), rel_tol=1e-9)


def test_cosine_zero_vector_left_returns_zero():
    assert _cosine([0.0, 0.0], [1.0, 2.0]) == 0.0


def test_cosine_zero_vector_right_returns_zero():
    assert _cosine([1.0, 2.0], [0.0, 0.0]) == 0.0


def test_cosine_zip_truncates_to_shorter_length():
    # zip stops at the shorter operand; trailing component is ignored.
    assert _cosine([1.0, 0.0, 99.0], [1.0, 0.0]) == 1.0


async def test_worker_clearance_recheck_fails_closed_after_expiry():
    pool = _ClearancePool(False)

    try:
        await _require_eval_data_clearance(
            pool,
            user_id=7,
            collection_id=8,
            document_ids=[9],
        )
    except PermissionError as exc:
        assert "no longer valid" in str(exc)
    else:
        raise AssertionError("expired clearance was accepted")

    query, args = pool.connection.calls[0]
    assert "clock_timestamp()" in query
    assert args == (7, 8, [9])


async def test_worker_clearance_recheck_accepts_exact_current_scope():
    pool = _ClearancePool(True)

    await _require_eval_data_clearance(
        pool,
        user_id=7,
        collection_id=8,
        document_ids=[9, 10],
    )

    assert pool.connection.calls[0][1] == (7, 8, [9, 10])


# ── _score_strategy: retrieval metrics ──────────────────────────────────


async def test_score_strategy_perfect_hit_at_1():
    """Single query whose embedding exactly matches doc 1's only chunk."""
    embedder = _FakeEmbedder({"q-doc1": [1.0, 0.0]})
    chunks_by_doc = {1: [_chunk("c1")], 2: [_chunk("c2")]}
    chunk_embeddings_by_doc = {1: [[1.0, 0.0]], 2: [[0.0, 1.0]]}
    queries = [{"query": "q-doc1", "expected_doc_id": 1}]

    out = await _score_strategy(embedder, chunks_by_doc, chunk_embeddings_by_doc, queries)

    assert out["hit_at_1"] == 1.0
    assert out["hit_at_5"] == 1.0
    assert out["mrr"] == 1.0
    # Single batched embed call for the query text.
    assert embedder.calls == [["q-doc1"]]


async def test_score_strategy_miss_gives_zero_metrics():
    """Expected doc never appears in top-k -> all retrieval metrics 0."""
    embedder = _FakeEmbedder({"q": [1.0, 0.0]})
    chunks_by_doc = {1: [_chunk("c1")], 2: [_chunk("c2")]}
    chunk_embeddings_by_doc = {1: [[1.0, 0.0]], 2: [[0.0, 1.0]]}
    # Query lines up with doc 1, but we expect doc 2 (only top_k=1 retrieved).
    queries = [{"query": "q", "expected_doc_id": 2}]

    out = await _score_strategy(
        embedder, chunks_by_doc, chunk_embeddings_by_doc, queries, top_k=1
    )

    assert out["hit_at_1"] == 0.0
    assert out["hit_at_5"] == 0.0
    assert out["mrr"] == 0.0
    assert out["per_query"][0]["rank"] is None


async def test_score_strategy_rank_two_mrr_half_and_hit5_only():
    """Expected doc is the 2nd-ranked result: Hit@1=0, Hit@5=1, MRR=0.5."""
    embedder = _FakeEmbedder({"q": [1.0, 0.0]})
    # Two docs, single chunk each. Doc 1 is closer; expected is doc 2.
    chunks_by_doc = {1: [_chunk("c1")], 2: [_chunk("c2")]}
    chunk_embeddings_by_doc = {
        1: [[1.0, 0.0]],        # cosine 1.0 (rank 1)
        2: [[0.9, 0.4359]],     # high but lower cosine (rank 2)
    }
    queries = [{"query": "q", "expected_doc_id": 2}]

    out = await _score_strategy(embedder, chunks_by_doc, chunk_embeddings_by_doc, queries)

    pq = out["per_query"][0]
    assert pq["rank"] == 2
    assert pq["hit_at_1"] is False
    assert pq["hit_at_5"] is True
    assert out["hit_at_1"] == 0.0
    assert out["hit_at_5"] == 1.0
    assert out["mrr"] == 0.5


async def test_score_strategy_averages_across_queries():
    """Two queries: one perfect hit, one miss -> averaged to 0.5 / 0.5."""
    embedder = _FakeEmbedder({"hit": [1.0, 0.0], "miss": [1.0, 0.0]})
    chunks_by_doc = {1: [_chunk("c1")], 2: [_chunk("c2")]}
    chunk_embeddings_by_doc = {1: [[1.0, 0.0]], 2: [[0.0, 1.0]]}
    queries = [
        {"query": "hit", "expected_doc_id": 1},   # matches doc 1 -> hit
        {"query": "miss", "expected_doc_id": 2},  # ranks doc1 first -> miss@1
    ]

    out = await _score_strategy(
        embedder, chunks_by_doc, chunk_embeddings_by_doc, queries, top_k=1
    )

    assert out["hit_at_1"] == 0.5
    # doc 2 is not in top-1 for the miss query either.
    assert out["hit_at_5"] == 0.5
    # MRR: query1 rr=1.0, query2 rr=0.0 -> mean 0.5
    assert out["mrr"] == 0.5


async def test_score_strategy_chunk_stats_aggregation():
    """avg_chunk_tokens / chunks_per_doc / total_chunks math."""
    embedder = _FakeEmbedder({"q": [1.0, 0.0]})
    chunks_by_doc = {
        1: [_chunk("a", token_count=10), _chunk("b", token_count=20)],
        2: [_chunk("c", token_count=30)],
    }
    chunk_embeddings_by_doc = {
        1: [[1.0, 0.0], [0.0, 1.0]],
        2: [[0.5, 0.5]],
    }
    queries = [{"query": "q", "expected_doc_id": 1}]

    out = await _score_strategy(embedder, chunks_by_doc, chunk_embeddings_by_doc, queries)

    assert out["total_chunks"] == 3
    # (10+20+30)/3 = 20.0
    assert out["avg_chunk_tokens"] == 20.0
    # 3 chunks / 2 docs = 1.5
    assert out["chunks_per_doc"] == 1.5


async def test_score_strategy_no_judge_credential_means_no_judge_avg():
    embedder = _FakeEmbedder({"q": [1.0, 0.0]})
    chunks_by_doc = {1: [_chunk("c1")]}
    chunk_embeddings_by_doc = {1: [[1.0, 0.0]]}
    queries = [{"query": "q", "expected_doc_id": 1}]

    out = await _score_strategy(embedder, chunks_by_doc, chunk_embeddings_by_doc, queries)

    assert out["judge_avg"] is None
    assert out["judge_n_scored"] == 0
    assert out["per_query"][0]["judge_score"] is None


async def test_judge_call_rechecks_clearance_before_external_send(monkeypatch):
    embedder = _FakeEmbedder({"q": [1.0, 0.0]})
    called = False

    async def deny():
        raise PermissionError("expired")

    async def should_not_send(*args, **kwargs):
        nonlocal called
        called = True
        return 3

    monkeypatch.setattr("ingestion_worker.evaluator.score_one", should_not_send)

    try:
        await _score_strategy(
            embedder,
            {1: [_chunk("c1")]},
            {1: [[1.0, 0.0]]},
            [{"query": "q", "expected_doc_id": 1}],
            judge_credential=JudgeCredential(
                endpoint_url="https://judge.invalid",
                model_name="judge",
                api_key="secret",
            ),
            authorize_judge=deny,
        )
    except PermissionError as exc:
        assert str(exc) == "expired"
    else:
        raise AssertionError("judge send continued after clearance denial")
    assert called is False


async def test_score_strategy_per_query_payload_shape():
    """The per-query record carries the fields the UI / results blob needs."""
    embedder = _FakeEmbedder({"q": [1.0, 0.0]})
    chunks_by_doc = {7: [_chunk("c1")], 8: [_chunk("c2")]}
    chunk_embeddings_by_doc = {7: [[1.0, 0.0]], 8: [[0.0, 1.0]]}
    queries = [{"query": "q", "expected_doc_id": 7}]

    out = await _score_strategy(embedder, chunks_by_doc, chunk_embeddings_by_doc, queries)

    pq = out["per_query"][0]
    assert pq["query"] == "q"
    assert pq["expected_doc_id"] == 7
    assert pq["top_doc_ids"][0] == 7  # closest chunk belongs to doc 7
    assert set(pq.keys()) == {
        "query",
        "expected_doc_id",
        "top_doc_ids",
        "rank",
        "hit_at_1",
        "hit_at_5",
        "judge_score",
    }


async def test_score_strategy_empty_queries_no_zero_division():
    """n falls back to 1 so empty query list yields 0.0 metrics, not a crash."""
    embedder = _FakeEmbedder({})
    chunks_by_doc = {1: [_chunk("c1")]}
    chunk_embeddings_by_doc = {1: [[1.0, 0.0]]}

    out = await _score_strategy(embedder, chunks_by_doc, chunk_embeddings_by_doc, [])

    assert out["hit_at_1"] == 0.0
    assert out["hit_at_5"] == 0.0
    assert out["mrr"] == 0.0
    assert out["per_query"] == []
    # embed still called once, with an empty batch.
    assert embedder.calls == [[]]


async def test_score_strategy_rounding_precision():
    """Three queries with one hit -> 1/3 rounded to 4 dp = 0.3333."""
    embedder = _FakeEmbedder({"q1": [1.0, 0.0], "q2": [1.0, 0.0], "q3": [1.0, 0.0]})
    chunks_by_doc = {1: [_chunk("c1")], 2: [_chunk("c2")]}
    chunk_embeddings_by_doc = {1: [[1.0, 0.0]], 2: [[0.0, 1.0]]}
    queries = [
        {"query": "q1", "expected_doc_id": 1},  # hit
        {"query": "q2", "expected_doc_id": 2},  # miss@1
        {"query": "q3", "expected_doc_id": 2},  # miss@1
    ]

    out = await _score_strategy(
        embedder, chunks_by_doc, chunk_embeddings_by_doc, queries, top_k=1
    )

    assert out["hit_at_1"] == round(1 / 3, 4)
