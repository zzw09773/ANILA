"""Semantic search REST API endpoint.

Endpoint:
    POST /search   Query the vector index and return top-k citations.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..config import settings

router = APIRouter(prefix="/search", tags=["search"])

_embedding_provider: Any = None
_retrieval_provider: Any = None


def set_search_providers(embedder: Any, retriever: Any) -> None:
    """Called from app factory to inject dependencies."""
    global _embedding_provider, _retrieval_provider
    _embedding_provider = embedder
    _retrieval_provider = retriever


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="Natural language search query")
    top_k: int = Field(default=5, ge=1, le=50, description="Maximum results to return")
    min_score: float = Field(default=0.0, ge=0.0, le=1.0, description="Minimum similarity score")
    user_id: str = Field(default="default")
    project_id: str = Field(default="default")
    include_parent_context: Optional[bool] = Field(
        default=None,
        description="Attach enclosing parent chunk content to each citation "
                    "(defaults to settings.rag_include_parent_context).",
    )


@router.post("", summary="Semantic search over indexed documents")
async def semantic_search(req: SearchRequest) -> JSONResponse:
    """Embed the query and retrieve the top-k most similar citations."""
    if _embedding_provider is None or _retrieval_provider is None:
        raise HTTPException(status_code=503, detail="Search providers not available")

    try:
        embeddings = await _embedding_provider.embed([req.query], input_type="query")
        query_embedding: list[float] = embeddings[0]
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Embedding failed: {exc}") from exc

    include_parent = (
        req.include_parent_context
        if req.include_parent_context is not None
        else settings.rag_include_parent_context
    )

    try:
        citations = await _retrieval_provider.search(
            query_embedding=query_embedding,
            user_id=req.user_id,
            project_id=req.project_id,
            top_k=req.top_k,
            min_score=req.min_score,
            include_parent_context=include_parent,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Retrieval failed: {exc}") from exc

    results = [
        {
            "chunk_id": c.chunk_id,
            "document_id": c.document_id,
            "document_title": c.document_title,
            "source_path": c.source_path,
            "format": c.format,
            "chunk_type": c.chunk_type.value if hasattr(c.chunk_type, "value") else str(c.chunk_type),
            "chunk_level": c.chunk_level,
            "heading_path": c.heading_path,
            "page": c.page,
            "confidence": c.confidence,
            "content": c.content,
            "parent_chunk_id": c.parent_chunk_id,
            "parent_content": c.parent_content,
            "citation": c.cite(),
            "metadata": c.metadata,
        }
        for c in citations
    ]

    return JSONResponse(content={
        "query": req.query,
        "results": results,
        "total": len(results),
    })
