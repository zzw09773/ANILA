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

3. Pick the strategy with highest Hit@1 (tiebreak by MRR, then by
   ``judge_avg`` when the judge LLM ran) as ``recommended_strategy``
   and write everything back.

Cost: 1 query embedding + 1 doc-chunk-batch embedding per strategy.
For 10 docs × 6 strategies × 50 chunks/strategy/doc, total chunk
embeddings = 3000 × N_strategies. Realistically a 6-strategy run is
~10–30 seconds depending on embedder latency.

Sprint 5 / Chunk X: optional LLM-as-judge axis. When the eval run
row carries ``judge_llm_config = {"credential_id": <user_llm_credentials.id>}``
the worker decrypts the credential via ``anila_security`` and
scores each (query, top-k chunks) pair 1–3 with the judge LLM (see
``judge.py``). Failed judge calls are skipped — the run still yields
retrieval metrics. The judge LLM call goes through the CSP proxy so
``token_usage`` rows get written with ``request_type='judge'``.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any
from anila_security import verify_queue_proof

from anila_core.ingestion.chunking_plugins import ChunkResult, get_chunker
from anila_core.ingestion.chunking_plugins.builtins import SemanticChunker
from anila_core.storage.adapters.pg_pool import PgPool

from ingestion_worker.document_io import read_and_extract
from ingestion_worker.embedder import Embedder
from ingestion_worker.judge import JudgeCredential, load_judge_credential, score_one
from ingestion_worker.settings import settings


logger = logging.getLogger(__name__)


_EVAL_CLEARANCE_SQL = """
WITH requested(document_id) AS (
    SELECT unnest($3::int[])
), scoped AS (
    SELECT d.id AS document_id,
           d.collection_id,
           d.classification_level AS document_level,
           c.classification_level AS collection_level,
           c.created_by AS collection_owner_id
      FROM requested r
      JOIN ingestion_documents d ON d.id = r.document_id
      JOIN ingestion_collections c ON c.id = d.collection_id
     WHERE d.collection_id = $2
), required AS (
    SELECT s.document_id, crc.compartment_id
      FROM scoped s
      JOIN collection_required_compartments crc
        ON crc.collection_id = s.collection_id
    UNION
    SELECT s.document_id, drc.compartment_id
      FROM scoped s
      JOIN document_required_compartments drc
        ON drc.document_id = s.document_id
), invalid_required AS (
    SELECT 1
      FROM required r
      LEFT JOIN security_compartments sc ON sc.id = r.compartment_id
     WHERE sc.id IS NULL
        OR NOT sc.is_active
        OR sc.code !~ '^[A-Z0-9][A-Z0-9_.-]{0,63}$'
     LIMIT 1
)
SELECT EXISTS (
           SELECT 1 FROM users u WHERE u.id = $1 AND u.is_active
       )
   AND (SELECT count(*) FROM scoped) = cardinality($3::int[])
   AND NOT EXISTS (
       SELECT 1
         FROM scoped s
        WHERE array_position(
                  ARRAY['無機密','營業秘密','機密','極機密','絕對機密']::text[],
                  s.collection_level
              ) IS NULL
           OR array_position(
                  ARRAY['無機密','營業秘密','機密','極機密','絕對機密']::text[],
                  s.document_level
              ) IS NULL
   )
   AND NOT EXISTS (
       SELECT 1
         FROM clearance_grants cg
        WHERE cg.subject_user_id = $1
          AND array_position(
                  ARRAY['無機密','營業秘密','機密','極機密','絕對機密']::text[],
                  cg.max_classification_level
              ) IS NULL
   )
   AND NOT EXISTS (SELECT 1 FROM invalid_required)
   AND NOT EXISTS (
       SELECT 1
         FROM scoped s
        WHERE NOT EXISTS (
            SELECT 1
              FROM clearance_grants cg
              JOIN collection_access_grants cag
                ON cag.clearance_grant_id = cg.id
               AND cag.collection_id = s.collection_id
               AND cag.revoked_at IS NULL
               AND cag.need_to_know
               AND (s.collection_owner_id = $1 OR cag.membership_granted)
             WHERE cg.subject_user_id = $1
               AND cg.revoked_at IS NULL
               AND cg.valid_from <= clock_timestamp()
               AND clock_timestamp() < cg.expires_at
               AND array_position(
                     ARRAY['無機密','營業秘密','機密','極機密','絕對機密']::text[],
                     cg.max_classification_level
                   ) >= GREATEST(
                     array_position(
                       ARRAY['無機密','營業秘密','機密','極機密','絕對機密']::text[],
                       s.collection_level
                     ),
                     array_position(
                       ARRAY['無機密','營業秘密','機密','極機密','絕對機密']::text[],
                       s.document_level
                     )
                   )
               AND NOT EXISTS (
                   SELECT 1
                     FROM required r
                    WHERE r.document_id = s.document_id
                      AND NOT EXISTS (
                          SELECT 1
                            FROM clearance_grant_compartments cgc
                           WHERE cgc.clearance_grant_id = cg.id
                             AND cgc.compartment_id = r.compartment_id
                      )
               )
        )
   )
"""


async def _require_eval_data_clearance(
    pool: PgPool,
    *,
    user_id: int | None,
    collection_id: int,
    document_ids: list[int],
) -> None:
    """Re-evaluate canonical clearance at each sensitive worker boundary.

    The queued row is not an authority token.  This query deliberately
    requires one currently-active grant to satisfy classification, all
    compartments, collection membership and need-to-know for every document.
    ``clock_timestamp()`` avoids PostgreSQL's transaction-start timestamp when
    a long evaluator run crosses a grant expiry boundary.
    """

    if user_id is None or user_id <= 0 or not document_ids:
        raise PermissionError("eval run lacks an attributable data principal")
    async with pool.acquire() as conn:
        allowed = await conn.fetchval(
            _EVAL_CLEARANCE_SQL,
            user_id,
            collection_id,
            document_ids,
        )
    if allowed is not True:
        raise PermissionError("eval run data clearance is no longer valid")


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
    pool: PgPool, doc_ids: list[int], collection_id: int
) -> list[dict[str, Any]]:
    """Fetch storage_path / filename / mime for each sample doc.

    Sprint 5 X security review (M4): the CSP API checks docs belong to
    the collection at create-time, but the worker re-checks here as
    defense-in-depth. Without this filter, a tampered eval_run row
    (e.g. via direct SQL INSERT, future bug, or transient race) could
    cause the worker to read file blobs from another tenant's
    collection — and with judge enabled, exfil them to the user's
    LLM. Cheap to add the WHERE clause; expensive to discover later.
    """
    sql = """
        SELECT id, filename, mime_type, storage_path
          FROM ingestion_documents
         WHERE id = ANY($1::int[])
           AND collection_id = $2
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, doc_ids, collection_id)
    return [dict(r) for r in rows]


async def _chunk_doc(
    text: str,
    parse_meta: dict,
    strategy_name: str,
    params: dict,
    embedder: Embedder,
    *,
    billing_user_id: int | None = None,
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
            chunk_params["_embeddings"] = await embedder.embed(
                segments, user_id=billing_user_id
            )
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
    *,
    billing_user_id: int | None = None,
    judge_credential: JudgeCredential | None = None,
    judge_top_k: int = 5,
    authorize_judge: Callable[[], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Run every query against this strategy's chunks and average.

    Sprint 5 X: when ``judge_credential`` is provided, also runs a
    judge-LLM scoring pass over the top-k chunks per query and reports
    ``judge_avg`` (mean of 1–3 scores). Failed judge calls are
    skipped from the average rather than counted as 0; if every call
    fails, ``judge_avg`` is None.
    """
    # Embed queries in a single batch (counted as evaluator usage).
    query_texts = [q["query"] for q in queries]
    query_embeddings = await embedder.embed(query_texts, user_id=billing_user_id)

    # Flatten (doc_id, chunk, embedding) for scoring.
    flat: list[tuple[int, ChunkResult, list[float]]] = []
    for doc_id, chunks in chunks_by_doc.items():
        embeds = chunk_embeddings_by_doc[doc_id]
        for c, e in zip(chunks, embeds):
            flat.append((doc_id, c, e))

    hits_at_1 = 0
    hits_at_5 = 0
    rr_total = 0.0
    judge_scores: list[int] = []
    per_query: list[dict[str, Any]] = []

    for q, q_emb in zip(queries, query_embeddings):
        scored = sorted(
            ((idx, _cosine(q_emb, e)) for idx, (_, _, e) in enumerate(flat)),
            key=lambda x: x[1],
            reverse=True,
        )[:top_k]
        top_doc_ids = [flat[idx][0] for idx, _ in scored]
        top_chunks = [flat[idx][1] for idx, _ in scored]

        expected = q["expected_doc_id"]
        h1 = top_doc_ids[:1].count(expected) > 0 if top_doc_ids else False
        h5 = expected in top_doc_ids[:5]
        rank = next(
            (i + 1 for i, d in enumerate(top_doc_ids) if d == expected),
            None,
        )
        rr = 1.0 / rank if rank else 0.0

        if h1:
            hits_at_1 += 1
        if h5:
            hits_at_5 += 1
        rr_total += rr

        # ── Judge phase (optional) ──────────────────────────────────
        # Score the top ``judge_top_k`` chunks; if the judge LLM is
        # unreachable / parses failure, ``score_one`` returns None
        # and we just skip this query's score.
        per_q_judge: int | None = None
        if judge_credential is not None:
            if authorize_judge is None:
                raise PermissionError("judge inference lacks runtime clearance check")
            await authorize_judge()
            judge_chunks = [c.content for c in top_chunks[:judge_top_k]]
            per_q_judge = await score_one(
                judge_credential, q["query"], judge_chunks
            )
            if per_q_judge is not None:
                judge_scores.append(per_q_judge)

        per_query.append({
            "query": q["query"],
            "expected_doc_id": expected,
            "top_doc_ids": top_doc_ids,
            "rank": rank,
            "hit_at_1": h1,
            "hit_at_5": h5,
            "judge_score": per_q_judge,
        })

    n = len(queries) or 1
    total_chunks = sum(len(c) for c in chunks_by_doc.values())
    judge_avg = (
        round(sum(judge_scores) / len(judge_scores), 3)
        if judge_scores
        else None
    )
    return {
        "hit_at_1": round(hits_at_1 / n, 4),
        "hit_at_5": round(hits_at_5 / n, 4),
        "mrr": round(rr_total / n, 4),
        "judge_avg": judge_avg,
        "judge_n_scored": len(judge_scores),
        "avg_chunk_tokens": round(
            sum(c.token_count for chunks in chunks_by_doc.values() for c in chunks)
            / max(1, total_chunks),
            1,
        ),
        "chunks_per_doc": round(total_chunks / max(1, len(chunks_by_doc)), 1),
        "total_chunks": total_chunks,
        "per_query": per_query,
    }


async def evaluate_strategies(
    ctx: dict, eval_run_id: int, queue_proof: str | None = None
) -> dict:
    """Arq handler. Runs every strategy in the run row, writes results."""
    pool: PgPool = ctx["pool"]
    embedder: Embedder = ctx["embedder"]
    if settings.ingestion_queue_hmac_key:
        verify_queue_proof(
            settings.ingestion_queue_hmac_key,
            task_name="evaluate_strategies",
            payload={"eval_run_id": eval_run_id},
            proof=queue_proof,
        )

    started = time.time()

    # 1. Load the eval run row.
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, collection_id, sample_document_ids, strategies_tried,
                   queries, arq_job_id, created_by, judge_llm_config
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
        # Sprint 5 X / M4: keep collection_id in scope so _load_sample_docs
        # can filter by it (defense-in-depth — see helper docstring).
        run_collection_id = int(row["collection_id"])
        strategies = row["strategies_tried"]
        queries = row["queries"]
        billing_user_id = row["created_by"]  # Sprint 4 V: usage attribution

        async def require_current_clearance() -> None:
            await _require_eval_data_clearance(
                pool,
                user_id=billing_user_id,
                collection_id=run_collection_id,
                document_ids=sample_doc_ids,
            )

        # Authority can expire between API enqueue and worker pickup.  Deny
        # before the first raw-file read rather than treating the queued row as
        # a durable capability.
        await require_current_clearance()
        # Sprint 5 X: optional LLM-as-judge.
        # ``judge_llm_config = {"credential_id": <int>, "top_k": 5}``
        judge_cfg = row["judge_llm_config"] or {}
        judge_credential = None
        # Sprint 5 X security review (H3): record decrypt failures so
        # tampering / wrong-key incidents aren't invisible. We log the
        # exception *type* only — never the exception value, which can
        # carry plaintext fragments or key bytes for some crypto errors.
        judge_load_error: str | None = None
        if (
            (
                not settings.anila_pilot_mode
                or settings.gate2_allow_unconverged_inference
            )
            and isinstance(judge_cfg, dict)
            and judge_cfg.get("credential_id")
        ):
            cred_id = int(judge_cfg["credential_id"])
            try:
                judge_credential = await load_judge_credential(pool, cred_id)
            except LookupError:
                judge_load_error = "credential_not_found"
                logger.warning(
                    "judge credential id=%s not found — judge skipped",
                    cred_id,
                )
            except Exception as exc:
                # Soft failure: retrieval metrics still produced. We
                # surface the exception class on the eval_run so the
                # operator can correlate (e.g. InvalidTag = ciphertext
                # tampered or SECRET_KEY rotated; RuntimeError = env
                # var missing).
                judge_load_error = type(exc).__name__
                logger.warning(
                    "judge credential id=%s decrypt failed (%s) — judge skipped",
                    cred_id,
                    type(exc).__name__,
                )

        # 2. Load all sample docs once (raw blobs from disk).
        # Per-doc parse errors are recorded so the operator can see WHY
        # a doc was skipped — silently swallowing them previously hid
        # missing-parser-stack failures behind a generic "0 chunks" error.
        docs = await _load_sample_docs(pool, sample_doc_ids, run_collection_id)
        if len(docs) != len(set(sample_doc_ids)):
            raise PermissionError("eval run document scope changed before blob read")
        parsed_docs: dict[int, tuple[str, dict]] = {}
        parse_errors: dict[int, str] = {}
        for d in docs:
            # File bytes are outside the database transaction, so evaluate
            # again at the last practical instant before each raw blob read.
            await require_current_clearance()
            doc_id = int(d["id"])
            sp = d["storage_path"]
            if not sp:
                parse_errors[doc_id] = "missing storage_path"
                continue
            try:
                text, parse_meta, _images = await read_and_extract(
                    sp,
                    d["filename"],
                    d["mime_type"],
                    timeout_seconds=settings.parse_timeout_seconds,
                )
                parsed_docs[doc_id] = (text, parse_meta)
            except Exception as exc:
                parse_errors[doc_id] = f"{type(exc).__name__}: {str(exc)[:200]}"
                logger.warning(
                    "eval %s: parse failed for doc %s: %s",
                    eval_run_id, doc_id, parse_errors[doc_id],
                )

        # 3. Per-strategy scoring.
        per_strategy: dict[str, Any] = {}
        for spec in strategies:
            # Recheck before each round of embedding work; in particular this
            # prevents already-parsed document bytes from continuing to flow
            # after a grant expires during a long run.
            await require_current_clearance()
            sname = spec.get("name")
            sparams = spec.get("params", {})
            chunks_by_doc: dict[int, list[ChunkResult]] = {}
            embeds_by_doc: dict[int, list[list[float]]] = {}

            for doc_id, (text, parse_meta) in parsed_docs.items():
                try:
                    chunks = await _chunk_doc(
                        text, parse_meta, sname, sparams, embedder,
                        billing_user_id=billing_user_id,
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
                        [c.content for c in chunks], user_id=billing_user_id,
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
                embedder, chunks_by_doc, embeds_by_doc, queries,
                billing_user_id=billing_user_id,
                judge_credential=judge_credential,
                judge_top_k=int(judge_cfg.get("top_k", 5)) if isinstance(judge_cfg, dict) else 5,
                authorize_judge=require_current_clearance,
            )
            per_strategy[sname] = metrics

        # 4. Pick recommended strategy.
        # Default tiebreak: Hit@1 then MRR.
        # Sprint 5 X: when judge ran successfully on this strategy,
        # add judge_avg as the *third* tiebreak — retrieval still wins
        # the primary axis (judge can hallucinate, Hit@1 can't), but a
        # higher-quality retrieval set breaks ties between two strategies
        # that produce identical Hit@1/MRR.
        valid = {
            k: v for k, v in per_strategy.items()
            if isinstance(v, dict) and "hit_at_1" in v
        }
        recommended = (
            max(
                valid.items(),
                key=lambda kv: (
                    kv[1]["hit_at_1"],
                    kv[1]["mrr"],
                    kv[1].get("judge_avg") or 0.0,
                ),
            )[0]
            if valid
            else None
        )

        results_payload = {
            "per_strategy": per_strategy,
            "elapsed_seconds": round(time.time() - started, 2),
            "n_docs": len(parsed_docs),
            "n_queries": len(queries),
            # Per-doc parse failures (doc_id → "ExceptionType: message").
            # Empty when every sample parsed cleanly.
            "parse_errors": parse_errors,
            # Surfaces a judge-credential decrypt / lookup failure
            # (None when judge wasn't requested or worked fine).
            "judge_load_error": judge_load_error,
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
