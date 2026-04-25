"""AgenticRAG LLM-callable tool surface.

Phase 2 Sprint 1 / Chunk F:
  The legacy ``create_vector_search_tool`` / ``create_keyword_search_tool``
  / ``create_read_document_tool`` factories were retired. They wrapped
  inline SQL against the pre-0014 schema (``user_id``, ``project_id``,
  ``chunk_type`` columns) which no longer exists — every query against
  the new ``document_chunks`` shape would fail at parse time.

  Re-implemented "tool-driven retrieval" tools belong in a follow-up
  sprint and will compose ``anila_core.storage.adapters.
  AgentScopedPgVectorStore`` rather than inline SQL. Until then, agents
  drive retrieval through ``api.py``'s ``retrieve_context`` (pre-process
  injection style) which has been migrated to the central SDK.

What survives here is the cross-encoder rerank helper, which is
SQL-free and reused by ``api.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from ..providers.reranker import RerankCandidate, Reranker

logger = logging.getLogger(__name__)


async def _arerank_candidates(
    reranker: Reranker,
    query: str,
    pool: list[dict[str, Any]],
    top_k: int,
    content_key: str = "content",
    chunk_id_key: str = "chunk_id",
) -> list[dict[str, Any]]:
    """Cross-encoder rerank a candidate pool, returning the top_k items.

    On any failure, returns ``pool[:top_k]`` unchanged so retrieval never
    silently breaks because of a flaky reranker.
    """
    if not pool:
        return []
    try:
        candidates = [
            RerankCandidate(
                chunk_id=str(item.get(chunk_id_key) or i),
                content=item.get(content_key, "") or "",
                metadata={},
                original_score=None,
            )
            for i, item in enumerate(pool)
        ]
        reranked = await reranker.rerank(query, candidates, top_k=top_k)
        if not reranked:
            return pool[:top_k]
        by_cid = {
            str(item.get(chunk_id_key) or i): item for i, item in enumerate(pool)
        }
        out: list[dict[str, Any]] = []
        for r in reranked:
            src = by_cid.get(r.candidate.chunk_id)
            if src is None:
                continue
            out.append({**src, "rerank_score": round(float(r.score), 4)})
        return out or pool[:top_k]
    except Exception as exc:
        logger.warning("tool reranker failed (%s) — using original order", exc)
        return pool[:top_k]


__all__ = ["_arerank_candidates"]
