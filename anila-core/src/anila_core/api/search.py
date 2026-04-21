"""Semantic search REST API endpoint.

Endpoint:
    POST /search   Query the vector index and return top-k chunks.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

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


class SearchResult(BaseModel):
    chunk_id: str
    document_id: str
    score: float
    content: str
    metadata: dict


@router.post("", summary="Semantic search over indexed documents")
async def semantic_search(req: SearchRequest) -> JSONResponse:
    """Embed the query and retrieve the top-k most similar document chunks."""
    if _embedding_provider is None or _retrieval_provider is None:
        raise HTTPException(status_code=503, detail="Search providers not available")

    # Embed the query
    try:
        embeddings = await _embedding_provider.embed([req.query], input_type="query")
        query_embedding: list[float] = embeddings[0]
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Embedding failed: {exc}") from exc

    # Retrieve top-k chunks
    try:
        chunks = await _retrieval_provider.search(
            query_embedding=query_embedding,
            user_id=req.user_id,
            project_id=req.project_id,
            top_k=req.top_k,
            min_score=req.min_score,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Retrieval failed: {exc}") from exc

    results = [
        {
            "chunk_id": c.chunk_id,
            "document_id": c.document_id,
            "score": c.metadata.get("score", 0.0),
            "content": c.content,
            "metadata": {k: v for k, v in c.metadata.items() if k != "score"},
        }
        for c in chunks
    ]

    return JSONResponse(content={
        "query": req.query,
        "results": results,
        "total": len(results),
    })
