"""AgenticRAG LLM-callable tool surface.

Phase 0 (2026-05-02): the tool factories now consume the local
``AgentScopedPgVectorStore`` (alias of ``CollectionScopedPgVectorStore``)
from ``agentic_rag.storage.adapters``. AgenticRAG is a fork-template and
must not import platform-internal anila-core packages.

Three tools:

- ``vector_search(query, top_k)`` — embed query, similarity search.
- ``keyword_search(query, top_k)`` — FTS over content_tsv.
- ``read_document(document_id, max_chunks)`` — full document text.

The factories return ``ToolDefinition`` objects ready to register on
the per-request ``ToolRegistry`` in ``server.py``'s ``/agentic-chat``.

The cross-encoder rerank helper survives unchanged.

Known issue (pre-existing, Sprint 2 deliverable to fix): the
``collection_id=`` kwarg passed below to ``store.similarity_search`` /
``store.keyword_search`` is not part of the current store API
(collection scope is constructor-level). These factories are not yet
wired into ``server.create_app``'s /agentic-chat path so the breakage
is dormant; Sprint 2 will reconcile signatures.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

import httpx

from ..storage.adapters import AgentScopedPgVectorStore

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


# ── Embedder helper ─────────────────────────────────────────────────────────


# Type alias for the "embed one query" callable that vector_search needs.
# Keeping it minimal-surface so callers can pass any async function with
# the right signature, including a stub for tests.
EmbedFn = Callable[[str], Awaitable[list[float]]]


def _build_default_embedder(
    base_url: str, api_key: str, model: str, target_dim: int = 4000,
    verify_ssl: bool = False,
) -> EmbedFn:
    """Construct an OpenAI-compatible embed-one-query callable.

    Truncates to ``target_dim`` (4000 for the live halfvec(4000) schema).
    Stays sync to async with httpx — matches the worker's embedder
    pattern but per-call (tools are short-lived, not pooled).
    """
    async def _embed(text: str) -> list[float]:
        async with httpx.AsyncClient(verify=verify_ssl, timeout=30) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/embeddings",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": model, "input": [text], "input_type": "query"},
            )
            resp.raise_for_status()
            vec = resp.json()["data"][0]["embedding"]
        if len(vec) > target_dim:
            vec = vec[:target_dim]
        return vec

    return _embed


# ── Tool factories ─────────────────────────────────────────────────────────


def create_vector_search_tool(
    store: AgentScopedPgVectorStore,
    embedder: EmbedFn,
    *,
    default_top_k: int = 5,
    min_score: float = 0.0,
    reranker: Reranker | None = None,
    rerank_pool_multiplier: int = 3,
    collection_id: int | None = None,
) -> ToolDefinition:
    """Vector similarity search over the agent's chunks.

    Args:
      store: the request-scoped ``AgentScopedPgVectorStore`` (already
        pinned to ``RAG_AGENT_ID``).
      embedder: async ``str -> list[float]`` callback. Use
        ``_build_default_embedder`` or your own.
      reranker: optional cross-encoder; when set, fetches
        ``top_k * rerank_pool_multiplier`` then reranks down to top_k.
      collection_id: pin search to one collection. ``None`` = all of
        the agent's collections (RLS still applies at the agent boundary).
    """

    async def _impl(params: dict[str, Any], **_ctx: Any) -> dict:
        query = params.get("query", "").strip()
        if not query:
            return {"error": "query is required"}
        top_k = int(params.get("top_k", default_top_k))
        pool_k = top_k * rerank_pool_multiplier if reranker is not None else top_k

        try:
            embedding = await embedder(query)
        except Exception as exc:
            return {"error": f"embedding failed: {type(exc).__name__}: {exc}"}

        hits = await store.similarity_search(
            embedding,
            collection_id=collection_id,
            top_k=pool_k,
            min_score=min_score,
        )
        results = [
            {
                "chunk_id": h.chunk.id,
                "document_id": h.chunk.document_id,
                "chunk_key": h.chunk.chunk_key,
                "content": h.chunk.content,
                "metadata": h.chunk.metadata,
                "score": round(float(h.score), 4),
            }
            for h in hits
        ]
        if reranker is not None and len(results) > 1:
            results = await _arerank_candidates(reranker, query, results, top_k)
        else:
            results = results[:top_k]
        return {"results": results}

    return ToolDefinition(
        name="vector_search",
        description=(
            "Semantic vector search over the agent's knowledge collections. "
            "Returns the top-k most similar chunks for the query."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language query."},
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": default_top_k,
                },
            },
            "required": ["query"],
        },
        safety=ToolSafety.READ_ONLY,
        implementation=_impl,
    )


def create_keyword_search_tool(
    store: AgentScopedPgVectorStore,
    *,
    default_top_k: int = 5,
    reranker: Reranker | None = None,
    rerank_pool_multiplier: int = 3,
    collection_id: int | None = None,
    tokenizer: Callable[[str], str] | None = None,
) -> ToolDefinition:
    """Full-text keyword search via the central SDK's tsvector path.

    ``tokenizer``: callable that pre-tokenises the query. CJK callers
    should pass ``agentic_rag.ingestion.tokenize_zh.tokenize`` so
    plainto_tsquery sees space-separated tokens. Latin-only callers
    can leave it ``None``.
    """

    async def _impl(params: dict[str, Any], **_ctx: Any) -> dict:
        query = params.get("query", "").strip()
        if not query:
            return {"error": "query is required"}
        top_k = int(params.get("top_k", default_top_k))
        pool_k = top_k * rerank_pool_multiplier if reranker is not None else top_k

        tokenized = tokenizer(query) if tokenizer else None
        hits = await store.keyword_search(
            query=query,
            collection_id=collection_id,
            top_k=pool_k,
            tokenized_query=tokenized,
        )
        results = [
            {
                "chunk_id": h.chunk.id,
                "document_id": h.chunk.document_id,
                "chunk_key": h.chunk.chunk_key,
                "content": h.chunk.content,
                "metadata": h.chunk.metadata,
                "score": round(float(h.score), 4),
            }
            for h in hits
        ]
        if reranker is not None and len(results) > 1:
            results = await _arerank_candidates(reranker, query, results, top_k)
        else:
            results = results[:top_k]
        return {"results": results}

    return ToolDefinition(
        name="keyword_search",
        description=(
            "Full-text keyword search over the agent's knowledge collections. "
            "Returns chunks matching the query terms via PostgreSQL tsvector."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": default_top_k,
                },
            },
            "required": ["query"],
        },
        safety=ToolSafety.READ_ONLY,
        implementation=_impl,
    )


def create_read_document_tool(
    store: AgentScopedPgVectorStore,
    *,
    max_chunks: int = 200,
) -> ToolDefinition:
    """Read all chunks of one document, ordered by id.

    Useful for "summarize this document" / "answer based on document N"
    LLM patterns where vector search is too narrow. Caps at
    ``max_chunks`` so a malicious / confused LLM can't pull a multi-MB
    document into the prompt.
    """

    async def _impl(params: dict[str, Any], **_ctx: Any) -> dict:
        try:
            doc_id = int(params.get("document_id"))
        except (TypeError, ValueError):
            return {"error": "document_id (integer) is required"}

        chunks = await store.list_by_document(
            document_id=doc_id,
            limit=max_chunks,
        )
        return {
            "document_id": doc_id,
            "chunk_count": len(chunks),
            "chunks": [
                {
                    "chunk_id": c.id,
                    "chunk_key": c.chunk_key,
                    "content": c.content,
                    "metadata": c.metadata,
                }
                for c in chunks
            ],
        }

    return ToolDefinition(
        name="read_document",
        description=(
            "Read every chunk of one document by document_id. Returns "
            "the chunks in original order."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "integer",
                    "description": "ID of the document to read.",
                },
            },
            "required": ["document_id"],
        },
        safety=ToolSafety.READ_ONLY,
        implementation=_impl,
    )


__all__ = [
    "EmbedFn",
    "_arerank_candidates",
    "_build_default_embedder",
    "create_keyword_search_tool",
    "create_read_document_tool",
    "create_vector_search_tool",
]
