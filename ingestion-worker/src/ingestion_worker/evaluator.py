"""Chunking Evaluator handler (Sprint 3 / Chunk N).

For each eval run row:
1. Load each sample document's already-stored chunks via the
   CollectionScopedPgVectorStore — we use the *parsed text* as input
   to the candidate strategies (chunker is deterministic given input
   text, so re-parse isn't necessary; we just re-chunk).

   Optimisation: we don't have stored ``raw text`` per-doc. We do
   have stored *chunks* (the original ingestion produced them).
   Re-assembling text from existing chunks loses document structure
   when the original chunker is fixed-size. For Sprint 3 cut, we
   re-read the raw blob from disk via storage_path — same path the
   ingestion-worker uses.

2. For each strategy in ``strategies_tried``:
   a. Chunk every sample doc via that strategy.
   b. Embed every produced chunk in ONE batched call.
   c. For each query: embed query, compute cosine similarity vs
      every chunk, take top-k, score:
      - Hit@1: expected_doc_id appears in top-1?
      - Hit@5: in top-5?
      - MRR: 1 / rank where expected first appears, else 0.
   d. Average across queries.

3. Pick the strategy with highest Hit@1 (tiebreak by MRR) as
   ``recommended_strategy`` and write everything back.

Cost: 1 query embedding + 1 doc-chunk-batch embedding per strategy.
For 10 docs × 6 strategies × 50 chunks/strategy/doc, total chunk
embeddings = 3000 × N_strategies. Realistically a 6-strategy run is
~10–30 seconds depending on embedder latency.

LLM-as-judge scoring is NOT in this Sprint. The ``judge_llm_config``
column is read-only here; future commit will iterate over its
endpoint + use ``decrypt_credential`` to decrypt the API key.
"""

from __future__ import annotations

import math
import time
from typing import Any

from anila_core.ingestion.chunking_plugins import ChunkResult, get_chunker
from anila_core.ingestion.chunking_plugins.builtins import SemanticChunker
from anila_core.storage.adapters.pg_pool import PgPool

from ingestion_worker.embedder import Embedder
from ingestion_worker.parsers import extract_text


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. NV-embed-V2 is L2-normalised so this collapses
    to a dot product, but keep the full form for safety against
    non-normalised future embedders."""
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return dot / math.sqrt(na * nb)


async def _load_sample_docs(
    pool: PgPool, doc_ids: list[int]
) -> list[dict[str, Any]]:
    """Fetch storage_path / filename / mime for each sample doc."""
    sql = """
        SELECT id, filename, mime_type, storage_path
          FROM ingestion_documents
         WHERE id = ANY($1::int[])
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, doc_ids)
    return [dict(r) for r in rows]


async def _chunk_doc(
    text: str, parse_meta: dict, strategy_name: str, params: dict, embedder: Embedder
) -> list[ChunkResult]:
    """Apply one strategy to one doc, including the semantic-pre-embed
    pre-pass when needed."""
    chunker = get_chunker(strategy_name)
    chunk_params = dict(params)
    if getattr(chunker, "requires_embedder", False):
        min_tok = int(chunk_params.get("min_segment_tokens", 128))
        segments = SemanticChunker.split_segments(text, min_tokens=min_tok)
        chunk_params["_segments"] = segments
        if len(segments) >= 2:
            chunk_params["_embeddings"] = await embedder.embed(segments)
        elif len(segments) == 1:
            chunk_params["_embeddings"] = [[]]
        else:
            chunk_params["_embeddings"] = []
    return chunker.chunk(text, parse_meta, chunk_params)


async def _score_strategy(
    embedder: Embedder,
    chunks_by_doc: dict[int, list[ChunkResult]],
    chunk_embeddings_by_doc: dict[int, list[list[float]]],
    queries: list[dict[str, Any]],
    top_k: int = 10,
) -> dict[str, Any]:
    """Run every query against this strategy's chunks and average.

    Returns ``{hit_at_1, hit_at_5, mrr, avg_chunk_tokens, chunks_per_doc,
    per_query: [...]}``.
    """
    # Embed queries in a single batch.
    query_texts = [q["query"] for q in queries]
    query_embeddings = await embedder.embed(query_texts)

    # Flatten (doc_id, chunk, embedding) for scoring.
    flat: list[tuple[int, ChunkResult, list[float]]] = []
    for doc_id, chunks in chunks_by_doc.items():
        embeds = chunk_embeddings_by_doc[doc_id]
        for c, e in zip(chunks, embeds):
            flat.append((doc_id, c, e))

    hits_at_1 = 0
    hits_at_5 = 0
    rr_total = 0.0
    per_query: list[dict[str, Any]] = []

    for q, q_emb in zip(queries, query_embeddings):
        scored = sorted(
            ((idx, _cosine(q_emb, e)) for idx, (_, _, e) in enumerate(flat)),
            key=lambda x: x[1],
            reverse=True,
        )[:top_k]
        top_doc_ids = [flat[idx][0] for idx, _ in scored]

        expected = q["expected_doc_id"]
        h1 = top_doc_ids[:1].count(expected) > 0 if top_doc_ids else False
        h5 = expected in top_doc_ids[:5]
        # MRR: 1 / first-rank-doc-matches, else 0.
        rank = next(
            (i + 1 for i, d in enumerate(top_doc_ids) if d == expected),
            None,
        )
        rr = 1.0 / rank if rank else 0.0

        if h1: hits_at_1 += 1
        if h5: hits_at_5 += 1
        rr_total += rr

        per_query.append({
            "query": q["query"],
            "expected_doc_id": expected,
            "top_doc_ids": top_doc_ids,
            "rank": rank,
            "hit_at_1": h1,
            "hit_at_5": h5,
        })

    n = len(queries) or 1
    total_chunks = sum(len(c) for c in chunks_by_doc.values())
    return {
        "hit_at_1": round(hits_at_1 / n, 4),
        "hit_at_5": round(hits_at_5 / n, 4),
        "mrr": round(rr_total / n, 4),
        "avg_chunk_tokens": round(
            sum(c.token_count for chunks in chunks_by_doc.values() for c in chunks)
            / max(1, total_chunks),
            1,
        ),
        "chunks_per_doc": round(total_chunks / max(1, len(chunks_by_doc)), 1),
        "total_chunks": total_chunks,
        "per_query": per_query,
    }


async def evaluate_strategies(ctx: dict, eval_run_id: int) -> dict:
    """Arq handler. Runs every strategy in the run row, writes results."""
    pool: PgPool = ctx["pool"]
    embedder: Embedder = ctx["embedder"]

    started = time.time()

    # 1. Load the eval run row.
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, collection_id, sample_document_ids, strategies_tried,
                   queries, arq_job_id
              FROM ingestion_eval_runs
             WHERE id = $1
            """,
            eval_run_id,
        )
        if row is None:
            return {"error": f"eval run {eval_run_id} not found"}
        await conn.execute(
            """
            UPDATE ingestion_eval_runs
               SET status = 'running', started_at = now()
             WHERE id = $1
            """,
            eval_run_id,
        )

    try:
        sample_doc_ids = list(row["sample_document_ids"])
        strategies = row["strategies_tried"]
        queries = row["queries"]

        # 2. Load all sample docs once (raw blobs from disk).
        docs = await _load_sample_docs(pool, sample_doc_ids)
        parsed_docs: dict[int, tuple[str, dict]] = {}
        for d in docs:
            sp = d["storage_path"]
            if not sp:
                continue
            try:
                with open(sp, "rb") as f:
                    blob = f.read()
                text, parse_meta = extract_text(d["filename"], blob, d["mime_type"])
                parsed_docs[int(d["id"])] = (text, parse_meta)
            except Exception:
                # Skip unreadable docs; they'll show up as missing in
                # results but don't fail the run.
                continue

        # 3. Per-strategy scoring.
        per_strategy: dict[str, Any] = {}
        for spec in strategies:
            sname = spec.get("name")
            sparams = spec.get("params", {})
            chunks_by_doc: dict[int, list[ChunkResult]] = {}
            embeds_by_doc: dict[int, list[list[float]]] = {}

            for doc_id, (text, parse_meta) in parsed_docs.items():
                try:
                    chunks = await _chunk_doc(
                        text, parse_meta, sname, sparams, embedder
                    )
                except Exception as exc:
                    # Bad params / unsupported strategy → skip this strategy
                    # entirely so the others still produce results.
                    chunks = []
                    per_strategy[sname] = {
                        "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    }
                    chunks_by_doc.clear()
                    break
                chunks_by_doc[doc_id] = chunks
                if chunks:
                    embeds_by_doc[doc_id] = await embedder.embed(
                        [c.content for c in chunks]
                    )
                else:
                    embeds_by_doc[doc_id] = []

            if sname in per_strategy:  # error path above
                continue
            if not any(chunks_by_doc.values()):
                per_strategy[sname] = {
                    "error": "all sample documents produced 0 chunks",
                }
                continue

            metrics = await _score_strategy(
                embedder, chunks_by_doc, embeds_by_doc, queries
            )
            per_strategy[sname] = metrics

        # 4. Pick recommended strategy: highest Hit@1, tiebreak MRR.
        valid = {
            k: v for k, v in per_strategy.items()
            if isinstance(v, dict) and "hit_at_1" in v
        }
        recommended = (
            max(
                valid.items(),
                key=lambda kv: (kv[1]["hit_at_1"], kv[1]["mrr"]),
            )[0]
            if valid
            else None
        )

        results_payload = {
            "per_strategy": per_strategy,
            "elapsed_seconds": round(time.time() - started, 2),
            "n_docs": len(parsed_docs),
            "n_queries": len(queries),
        }

        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ingestion_eval_runs
                   SET status = 'succeeded',
                       results = $2::jsonb,
                       recommended_strategy = $3::text,
                       completed_at = now()
                 WHERE id = $1
                """,
                eval_run_id,
                results_payload,
                recommended,
            )
        return {"recommended": recommended, **results_payload}

    except Exception as exc:
        # Record the failure on the row so the dev sees what blew up.
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ingestion_eval_runs
                   SET status = 'failed',
                       error_code = 'E_INTERNAL',
                       error_message = $2::text,
                       completed_at = now()
                 WHERE id = $1
                """,
                eval_run_id,
                f"{type(exc).__name__}: {str(exc)[:500]}",
            )
        raise
