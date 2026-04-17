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

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool Implementations (closures over storage adapters)
# ---------------------------------------------------------------------------

def create_vector_search_tool(
    embedding_provider: Any,
    retrieval_provider: Any,
    user_id: str = "default",
    project_id: str = "default",
) -> ToolDefinition:
    """Create a vector_search tool backed by pgvector.

    The LLM calls this tool with a ``query`` string. The implementation
    embeds the query, searches pgvector, and returns top-k chunks.
    """

    async def _impl(params: dict[str, Any], **_ctx: Any) -> dict:
        query = params.get("query", "")
        top_k = int(params.get("top_k", 5))

        if not query:
            return {"error": "query is required"}

        try:
            embeddings = await embedding_provider.embed([query], input_type="query")
            query_vec = embeddings[0]
        except Exception as exc:
            logger.warning("vector_search embed failed: %s", exc)
            return {"error": f"Embedding failed: {exc}"}

        try:
            chunks = await retrieval_provider.search(
                query_embedding=query_vec,
                user_id=user_id,
                project_id=project_id,
                top_k=top_k,
                min_score=0.0,
            )
        except Exception as exc:
            logger.warning("vector_search retrieval failed: %s", exc)
            return {"error": f"Search failed: {exc}"}

        return {
            "results": [
                {
                    "chunk_id": c.chunk_id,
                    "document_id": c.document_id,
                    "content": c.content[:2000],
                    "score": round(float(c.metadata.get("score", 0.0)), 4),
                    "source": c.metadata.get("source_path", ""),
                }
                for c in chunks
            ],
            "total": len(chunks),
        }

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
) -> ToolDefinition:
    """Create a keyword_search tool using PostgreSQL trigram similarity.

    Uses pg_trgm for fuzzy keyword matching as a complement to vector search.
    Falls back to ILIKE when pg_trgm is unavailable.

    The *user_id* and *project_id* are captured in a closure so the LLM
    cannot override the scope — all queries are bounded to the caller's tenant.
    """

    async def _impl(params: dict[str, Any], **_ctx: Any) -> dict:
        query = params.get("query", "")
        top_k = int(params.get("top_k", 5))

        if not query:
            return {"error": "query is required"}

        try:
            async with db_pool.acquire() as conn:
                # Try pg_trgm first, fall back to ILIKE
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
                        query, top_k, user_id, project_id,
                    )
                except Exception:
                    # pg_trgm not available — fall back to ILIKE
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
                        like_pattern, top_k, user_id, project_id,
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
                    SELECT chunk_id, content, metadata
                    FROM document_chunks
                    WHERE document_id = $1
                      AND user_id = $2
                      AND project_id = $3
                    ORDER BY (metadata->>'chunk_index')::int NULLS LAST
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
            chunks.append({
                "chunk_id": row["chunk_id"],
                "content": row["content"],
                "heading": meta.get("heading", ""),
                "chunk_index": meta.get("chunk_index", 0),
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
