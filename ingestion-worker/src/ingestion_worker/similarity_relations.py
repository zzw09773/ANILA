"""Embedding-based topic-similarity edges (document-relations / C track).

A different KIND of edge from citations: pure vector similarity between
document-level centroids — surfaces docs that are *topically* related even with
no explicit citation. Written to the SAME ``document_relations`` table as
``source='similarity'``, ``relation_type='relates'``, ``confidence`` = cosine,
coexisting with rule/manual/llm rows (UNIQUE includes source).

Why top-K, not a threshold: same-domain corpora cluster tightly (regulations in
one domain routinely score 0.95+ pairwise), so an absolute threshold connects
everything. We take each document's K nearest neighbours instead — sparsity
comes from K; the floor only screens genuinely off-topic docs. The whole thing
is one pgvector query (``avg(halfvec)`` centroids + ``<=>`` + a per-source
``row_number()`` window), verified against pgvector 0.8.2.

Recomputed collection-wide on each ingest / reresolve because adding one
document shifts everyone's nearest neighbours. RLS-scoped like every other
``document_relations`` write.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Canonical (a < b) top-K nearest-neighbour pairs over document centroids.
_PAIRS_SQL = """
WITH centroids AS (
    SELECT document_id, avg(embedding) AS c
      FROM document_chunks
     WHERE collection_id = $1 AND chunk_type = 'leaf' AND embedding IS NOT NULL
     GROUP BY document_id
),
pairs AS (
    SELECT a.document_id AS src, b.document_id AS dst, 1 - (a.c <=> b.c) AS sim
      FROM centroids a JOIN centroids b ON a.document_id <> b.document_id
),
ranked AS (
    SELECT src, dst, sim,
           row_number() OVER (PARTITION BY src ORDER BY sim DESC) AS rnk
      FROM pairs
     WHERE sim >= $2
)
SELECT least(src, dst) AS a, greatest(src, dst) AS b, max(sim) AS sim
  FROM ranked
 WHERE rnk <= $3
 GROUP BY least(src, dst), greatest(src, dst)
"""


async def recompute_similarity_edges(
    pool: Any,
    *,
    collection_id: int,
    run_id: str | None,
    settings: Any,
) -> dict[str, int]:
    """Recompute the collection's ``source='similarity'`` edges. Returns
    ``{"edges": n}`` (0 when disabled / not enough docs / too many docs)."""
    if not settings.enable_similarity_edges:
        return {"edges": 0}

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                f"SET LOCAL anila.collection_id = {int(collection_id)}"
            )
            doc_count = await conn.fetchval(
                "SELECT count(DISTINCT document_id) FROM document_chunks "
                "WHERE collection_id = $1 AND chunk_type = 'leaf' AND embedding IS NOT NULL",
                collection_id,
            )
            if not doc_count or doc_count < 2:
                # nothing to relate yet; still clear stale similarity edges
                await conn.execute(
                    "DELETE FROM document_relations "
                    "WHERE collection_id = $1 AND source = 'similarity'",
                    collection_id,
                )
                return {"edges": 0}
            if doc_count > settings.similarity_max_docs:
                logger.info(
                    "collection %s: %d docs > similarity cap %d — skipping "
                    "similarity recompute (needs an ANN pre-filter)",
                    collection_id, doc_count, settings.similarity_max_docs,
                )
                return {"edges": 0}

            pairs = await conn.fetch(
                _PAIRS_SQL,
                collection_id,
                float(settings.similarity_min),
                int(settings.similarity_top_k),
            )

            # normalized_title per involved doc → durable target_ref
            ids = sorted({r["a"] for r in pairs} | {r["b"] for r in pairs})
            norm_by_id: dict[int, str] = {}
            if ids:
                trows = await conn.fetch(
                    "SELECT id, normalized_title FROM ingestion_documents "
                    "WHERE collection_id = $1 AND id = ANY($2::int[])",
                    collection_id, ids,
                )
                norm_by_id = {r["id"]: (r["normalized_title"] or "") for r in trows}

            # collection-wide replace: similarity edges are global, not per-src
            await conn.execute(
                "DELETE FROM document_relations "
                "WHERE collection_id = $1 AND source = 'similarity'",
                collection_id,
            )
            inserted = 0
            for r in pairs:
                a, b, sim = r["a"], r["b"], float(r["sim"])
                target_ref = norm_by_id.get(b) or f"doc:{b}"
                status = await conn.execute(
                    "INSERT INTO document_relations "
                    "(collection_id, src_document_id, dst_document_id, target_ref, "
                    " relation_type, confidence, source, extractor_run_id) "
                    "VALUES ($1, $2, $3, $4, 'relates', $5, 'similarity', $6) "
                    "ON CONFLICT (collection_id, src_document_id, target_ref, relation_type, source) "
                    "DO NOTHING",
                    collection_id, a, b, target_ref, sim, run_id,
                )
                if isinstance(status, str) and status.rsplit(" ", 1)[-1] == "1":
                    inserted += 1
    return {"edges": inserted}
