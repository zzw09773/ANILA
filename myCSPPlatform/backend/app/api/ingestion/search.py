"""Semantic search over a collection (`POST /api/ingestion/collections/{id}/search`).

Sprint 5 follow-up: ANILA LM (and any other consumer — AgenticRAG agents,
n8n nodes, future CLI) needs a single endpoint that:

  1. embeds the user's natural-language query through the same model as
     the collection's chunks (so vectors live in the same space),
  2. runs cosine similarity against ``document_chunks.embedding`` via
     the HNSW index, scoped under RLS to this collection,
  3. joins back to ``ingestion_documents`` for filename labelling,
  4. returns the top-K hits with content + score for citation rendering.

We deliberately do NOT compose retrieved context into a prompt here — the
caller (chat / Studio generator) decides how to inline citations. The
backend is a pure retrieval primitive.

Why a separate file from documents.py / collections.py:
  - Read-only retrieval is its own concern; no FK writes, no audit log
    (search is metered via ``request_type='embedding'`` already in
    ``token_usage`` because the embed call goes through the proxy).
  - Schemas (``SearchRequest`` / ``SearchHitOut``) live with the route
    that returns them.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from anila_core.storage.adapters.pgvector_store import (
    CollectionScopedPgVectorStore,
)

from app.api.ingestion.collections import _require_collection_access
from app.database import get_db
from app.models.ingestion import IngestionDocument
from app.models.model_registry import ModelRegistry
from app.models.user import User
from app.services.auth_service import get_current_user
from app.services.ingestion_pool import get_pool
from app.services.proxy_service import proxy_request

router = APIRouter(tags=["Ingestion / Search"])


# ── Schemas ─────────────────────────────────────────────────────────────────


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=4000, description="自然語言查詢")
    top_k: int = Field(
        default=5,
        ge=1,
        le=50,
        description="回傳前 N 個最相似的段落",
    )
    min_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="cosine 相似度最低門檻 (0=不過濾)；0.7 ≈ '至少七成相似'",
    )
    document_ids: list[int] | None = Field(
        default=None,
        description=(
            "可選：僅在指定 documents 內檢索（None=全 collection）。"
            "前端如要做 'in this doc' 之類的範圍縮限會用到。"
        ),
    )


class SearchHitOut(BaseModel):
    """Citation-ready hit. Strips the embedding (UI doesn't need 4000 floats)."""

    chunk_id: int
    document_id: int
    filename: str
    chunk_key: str
    content: str
    score: float = Field(..., description="cosine similarity in [0, 1]; higher = closer")
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    query: str
    embedding_model: str
    embedding_dim: int
    results: list[SearchHitOut]


# ── Helpers ─────────────────────────────────────────────────────────────────


async def _embed_query(
    db: Session,
    user: User,
    model_name: str,
    embedding_dim: int,
    query: str,
) -> list[float]:
    """Embed ``query`` through the same model as the collection.

    Reuses ``proxy_request`` so the call:
      - runs through the model_registry endpoint resolution (so the
        embedder URL change in one place propagates),
      - is metered into ``token_usage`` with request_type='embedding',
      - inherits the proxy's retry/backoff.

    Truncation logic mirrors ``ingestion_worker/embedder.py``:
    NV-embed-V2 returns 4096-d vectors but ``halfvec(4000)`` is what
    pgvector can index; drop the last 96. If the runtime model returns
    a shorter vector, that's a model-mismatch and we 422 — the chunks
    were embedded against a different model and the collection should
    be reindexed before search makes sense.
    """
    model = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.name == model_name)
        .first()
    )
    if model is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Collection's embedding_model '{model_name}' is not registered "
                "in model_registry — admin must add it before search works."
            ),
        )
    if not model.is_active:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Embedding model '{model_name}' is registered but inactive.",
        )

    body = {"model": model_name, "input": query}
    response = await proxy_request(
        model=model,
        api_key_id=None,  # SPA caller; usage attributes to user, no key
        user_id=user.id,
        department_id=user.department_id,
        request_body=body,
        endpoint_path="/v1/embeddings",
    )

    try:
        raw_vector = response["data"][0]["embedding"]
    except (KeyError, IndexError, TypeError) as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Embedding endpoint returned an unexpected payload shape.",
        ) from e

    if not isinstance(raw_vector, list) or not all(
        isinstance(x, (int, float)) for x in raw_vector
    ):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Embedding endpoint returned a non-numeric vector.",
        )

    if len(raw_vector) < embedding_dim:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Model returned {len(raw_vector)}-d vector but collection's "
                f"chunks are stored as {embedding_dim}-d. The collection was "
                "indexed against a different model — reindex before searching."
            ),
        )
    return [float(x) for x in raw_vector[:embedding_dim]]


# ── Endpoint ────────────────────────────────────────────────────────────────


@router.post(
    "/api/ingestion/collections/{collection_id}/search",
    response_model=SearchResponse,
)
async def search_collection(
    collection_id: int,
    payload: SearchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SearchResponse:
    """Semantic top-K retrieval over one collection's chunks.

    Auth: ``_require_collection_access`` — admin or owner. Sharing across
    users is a future ``collection_access_grants`` feature; for now the
    same gate as document upload / chunk inspect.
    """
    coll = _require_collection_access(db, current_user, collection_id)

    if coll.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Collection {collection_id} is {coll.status}; reactivate before search.",
        )

    query_vec = await _embed_query(
        db,
        current_user,
        coll.embedding_model,
        coll.embedding_dim,
        payload.query,
    )

    try:
        pool = get_pool()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    store = CollectionScopedPgVectorStore(pool, collection_id=coll.id)
    hits = await store.similarity_search(
        query_embedding=query_vec,
        top_k=payload.top_k,
        min_score=payload.min_score,
    )

    # Optional document_ids filter — done in app code rather than SQL
    # because ``similarity_search`` lives in anila_core and we don't want
    # to fork its signature for one consumer. The HNSW index makes the
    # initial scan cheap; post-filter on top_k is O(k).
    if payload.document_ids:
        allowed = set(payload.document_ids)
        hits = [h for h in hits if h.chunk.document_id in allowed]

    if not hits:
        return SearchResponse(
            query=payload.query,
            embedding_model=coll.embedding_model,
            embedding_dim=coll.embedding_dim,
            results=[],
        )

    # Bulk-fetch filenames for the hit document set. Single round-trip
    # vs N+1 lookups — handful of doc IDs at most (top_k ≤ 50).
    doc_ids = {h.chunk.document_id for h in hits}
    rows = (
        db.query(IngestionDocument.id, IngestionDocument.filename)
        .filter(IngestionDocument.id.in_(doc_ids))
        .all()
    )
    filenames = {r.id: r.filename for r in rows}

    return SearchResponse(
        query=payload.query,
        embedding_model=coll.embedding_model,
        embedding_dim=coll.embedding_dim,
        results=[
            SearchHitOut(
                chunk_id=h.chunk.id,
                document_id=h.chunk.document_id,
                filename=filenames.get(h.chunk.document_id, "<unknown>"),
                chunk_key=h.chunk.chunk_key,
                content=h.chunk.content,
                score=h.score,
                metadata=h.chunk.metadata or {},
            )
            for h in hits
        ],
    )
