"""RAG Tools — LLM-callable tools for Agentic RAG.

Provides ToolDefinition instances for:
- vector_search: semantic vector search via pgvector
- keyword_search: trigram / BM25-style keyword search
- read_document: read full document chunks by document ID

These tools wrap existing storage adapters so the LLM can drive
retrieval autonomously (tool-driven RAG) instead of relying on
pre-process injection.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..models.tool import ToolDefinition, ToolSafety
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
        by_cid = {str(item.get(chunk_id_key) or i): item for i, item in enumerate(pool)}
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


# ---------------------------------------------------------------------------
# Tool Implementations (closures over storage adapters)
# ---------------------------------------------------------------------------

def create_vector_search_tool(
    embedding_provider: Any,
    retrieval_provider: Any,
    user_id: str = "default",
    project_id: str = "default",
    reranker: Reranker | None = None,
    rerank_pool_multiplier: int = 3,
) -> ToolDefinition:
    """Create a vector_search tool backed by pgvector.

    The LLM calls this tool with a ``query`` string. The implementation
    embeds the query, searches pgvector, and returns top-k chunks.

    When *reranker* is provided, fetches ``top_k * rerank_pool_multiplier``
    candidates first then applies cross-encoder rerank to keep top_k.
    """

    async def _impl(params: dict[str, Any], **_ctx: Any) -> dict:
        from ..ingestion.normalize import normalize_zh

        raw_query = params.get("query", "")
        top_k = int(params.get("top_k", 5))

        if not raw_query:
            return {"error": "query is required"}

        query = normalize_zh(raw_query)
        pool_k = top_k * rerank_pool_multiplier if reranker is not None else top_k

        try:
            embeddings = await embedding_provider.embed([query], input_type="query")
            query_vec = embeddings[0]
        except Exception as exc:
            logger.warning("vector_search embed failed: %s", exc)
            return {"error": f"Embedding failed: {exc}"}

        try:
            citations = await retrieval_provider.search(
                query_embedding=query_vec,
                user_id=user_id,
                project_id=project_id,
                top_k=pool_k,
                min_score=0.0,
            )
        except Exception as exc:
            logger.warning("vector_search retrieval failed: %s", exc)
            return {"error": f"Search failed: {exc}"}

        items = [
            {
                "chunk_id": c.chunk_id,
                "document_id": c.document_id,
                "document_title": c.document_title,
                "content": c.content[:2000],
                "confidence": round(float(c.confidence), 4),
                "heading_path": c.heading_path,
                "page": c.page,
                "chunk_type": c.chunk_type.value if hasattr(c.chunk_type, "value") else str(c.chunk_type),
                "citation": c.cite(),
                "source": c.source_path,
            }
            for c in citations
        ]

        if reranker is not None and len(items) > 1:
            items = await _arerank_candidates(reranker, query, items, top_k)
        else:
            items = items[:top_k]

        return {"results": items, "total": len(items)}

    return ToolDefinition(
        name="vector_search",
        description="根據語義相似度搜索知識庫。適用於概念性問題、模糊查詢。",
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索查詢文字",
                },
                "top_k": {
                    "type": "number",
                    "description": "返回結果數量（預設 5）",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
        safety=ToolSafety.READ_ONLY,
        implementation=_impl,
    )


def create_keyword_search_tool(
    db_pool: Any,
    user_id: str = "default",
    project_id: str = "default",
    reranker: Reranker | None = None,
    rerank_pool_multiplier: int = 3,
) -> ToolDefinition:
    """Create a keyword_search tool using PostgreSQL FTS / trigram.

    Tries Traditional-Chinese-aware tsvector FTS first (filled in by the
    pgvector adapter via the ``content_tsv`` column), then pg_trgm
    similarity, then a final ILIKE fallback.

    The *user_id* and *project_id* are captured in a closure so the LLM
    cannot override the scope — all queries are bounded to the caller's tenant.

    When *reranker* is provided, a wider pool is fetched and cross-encoder
    rerank narrows it to ``top_k``.
    """

    async def _impl(params: dict[str, Any], **_ctx: Any) -> dict:
        from ..ingestion.normalize import normalize_zh
        from ..ingestion.tokenize_zh import tokenize as _tokenize

        raw_query = params.get("query", "")
        top_k = int(params.get("top_k", 5))

        if not raw_query:
            return {"error": "query is required"}

        query = normalize_zh(raw_query)
        pool_k = top_k * rerank_pool_multiplier if reranker is not None else top_k

        rows: list[Any] = []
        try:
            async with db_pool.acquire() as conn:
                # Tier 1: tsvector FTS via tokenize_zh (bigram or CKIP)
                try:
                    tsq = _tokenize(query)
                    if tsq.strip():
                        rows = await conn.fetch(
                            """
                            SELECT chunk_id, document_id, content, metadata,
                                   ts_rank_cd(content_tsv,
                                              plainto_tsquery('simple', $1)) AS score
                            FROM document_chunks
                            WHERE user_id = $3 AND project_id = $4
                              AND content_tsv @@ plainto_tsquery('simple', $1)
                            ORDER BY score DESC
                            LIMIT $2
                            """,
                            tsq, pool_k, user_id, project_id,
                        )
                except Exception as exc:
                    logger.debug("FTS path unavailable, falling back: %s", exc)
                    rows = []

                # Tier 2: pg_trgm similarity
                if not rows:
                    try:
                        rows = await conn.fetch(
                            """
                            SELECT chunk_id, document_id, content, metadata,
                                   similarity(content, $1) AS score
                            FROM document_chunks
                            WHERE user_id = $3 AND project_id = $4
                              AND content % $1
                            ORDER BY similarity(content, $1) DESC
                            LIMIT $2
                            """,
                            query, pool_k, user_id, project_id,
                        )
                    except Exception:
                        rows = []

                # Tier 3: ILIKE fallback
                if not rows:
                    like_pattern = f"%{query}%"
                    rows = await conn.fetch(
                        """
                        SELECT chunk_id, document_id, content, metadata,
                               0.5 AS score
                        FROM document_chunks
                        WHERE user_id = $3 AND project_id = $4
                          AND content ILIKE $1
                        LIMIT $2
                        """,
                        like_pattern, pool_k, user_id, project_id,
                    )
        except Exception as exc:
            logger.warning("keyword_search failed: %s", exc)
            return {"error": f"Search failed: {exc}"}

        results = []
        for row in rows:
            meta = row["metadata"] or {}
            if isinstance(meta, str):
                meta = json.loads(meta)
            results.append({
                "chunk_id": row["chunk_id"],
                "document_id": row["document_id"],
                "content": row["content"][:2000],
                "score": round(float(row["score"]), 4),
                "source": meta.get("source_path", ""),
            })

        if reranker is not None and len(results) > 1:
            results = await _arerank_candidates(reranker, query, results, top_k)
        else:
            results = results[:top_k]

        return {"results": results, "total": len(results)}

    return ToolDefinition(
        name="keyword_search",
        description="根據關鍵字精確匹配搜索知識庫。適用於特定術語、名稱、代碼等精確查詢。",
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "關鍵字查詢文字",
                },
                "top_k": {
                    "type": "number",
                    "description": "返回結果數量（預設 5）",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
        safety=ToolSafety.READ_ONLY,
        implementation=_impl,
    )


def create_read_document_tool(
    db_pool: Any,
    user_id: str = "default",
    project_id: str = "default",
) -> ToolDefinition:
    """Create a read_document tool that retrieves all chunks for a document.

    When the LLM sees a snippet from vector_search and wants the full
    document content, it can call this tool with the document_id.

    The *user_id* and *project_id* are captured in a closure — the LLM
    can only read documents that belong to the caller's tenant.
    """

    async def _impl(params: dict[str, Any], **_ctx: Any) -> dict:
        document_id = params.get("document_id", "")

        if not document_id:
            return {"error": "document_id is required"}

        try:
            async with db_pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT chunk_id, content, metadata,
                           chunk_type, chunk_level, heading_path
                    FROM document_chunks
                    WHERE document_id = $1
                      AND user_id = $2
                      AND project_id = $3
                      AND chunk_type IN ('content', 'image')
                    ORDER BY chunk_level, created_at
                    """,
                    document_id,
                    user_id,
                    project_id,
                )
        except Exception as exc:
            logger.warning("read_document failed: %s", exc)
            return {"error": f"Read failed: {exc}"}

        if not rows:
            return {"error": f"Document '{document_id}' not found"}

        chunks = []
        for row in rows:
            meta = row["metadata"] or {}
            if isinstance(meta, str):
                meta = json.loads(meta)
            heading_path = row["heading_path"] or []
            if isinstance(heading_path, str):
                heading_path = json.loads(heading_path)
            chunks.append({
                "chunk_id": row["chunk_id"],
                "content": row["content"],
                "chunk_type": row["chunk_type"],
                "heading_path": list(heading_path) if heading_path else [],
            })

        full_content = "\n\n---\n\n".join(c["content"] for c in chunks)

        return {
            "document_id": document_id,
            "total_chunks": len(chunks),
            "content": full_content[:16000],
            "truncated": len(full_content) > 16000,
        }

    return ToolDefinition(
        name="read_document",
        description="讀取文檔的完整內容。當向量搜尋結果中看到感興趣的片段時，用此工具讀取完整文件。",
        input_schema={
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "文檔 ID（從 vector_search 結果中取得）",
                },
            },
            "required": ["document_id"],
        },
        safety=ToolSafety.READ_ONLY,
        implementation=_impl,
    )
