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

from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
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

_search_bearer = HTTPBearer(auto_error=False)


@dataclass
class SearchPrincipal:
    """Effective principal for a search request.

    ``user`` is the identity we authorise against (for an agent csk- it is the
    agent's OWNER). ``agent`` is set only when the caller authenticated with an
    agent service token; the endpoint then hard-scopes it to
    ``agent.bound_collection_id`` (S-Q1, least privilege).
    """

    user: User
    agent: "object | None" = None


def resolve_search_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_search_bearer),
    db: Session = Depends(get_db),
) -> SearchPrincipal:
    """Search auth accepting EITHER a user (JWT/sk-/cookie) OR an agent csk-.

    csk- path (S-Q1): one credential now serves both inbound Router→agent auth
    and outbound RAG search. The token resolves to its agent; the effective
    user becomes the agent's owner; the endpoint enforces the bound collection.
    """
    token = credentials.credentials if (credentials and credentials.credentials) else None
    if token and token.startswith("csk-"):
        from app.models.agent import Agent
        from app.services import agent_credential_service

        identity = agent_credential_service.verify_service_token(db, token=token)
        if not identity or identity.kind != "agent" or not identity.agent_id:
            raise HTTPException(status_code=401, detail="無效的 service token")
        agent = db.query(Agent).filter(Agent.id == identity.agent_id).first()
        if agent is None:
            raise HTTPException(status_code=401, detail="service token 對應的 agent 不存在")
        owner = db.query(User).filter(User.id == agent.owner_user_id).first()
        if owner is None:
            raise HTTPException(status_code=401, detail="agent owner 不存在")
        return SearchPrincipal(user=owner, agent=agent)

    # User path — delegate to the existing resolver (Authorization header / sk- /
    # httpOnly cookie). Raises 401 when no valid user credential is present.
    user = get_current_user(request, credentials, db)
    return SearchPrincipal(user=user, agent=None)


def _enforce_agent_collection_scope(principal: SearchPrincipal, collection_id: int) -> None:
    """For the agent csk- path, reject any collection that isn't the agent's
    single bound collection. No-op for user principals."""
    agent = principal.agent
    if agent is not None and getattr(agent, "bound_collection_id", None) != collection_id:
        raise HTTPException(
            status_code=403,
            detail="此 agent 的憑證無權搜尋該 collection（僅限其綁定的 collection）",
        )


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
    # Sprint 9 X / parent-child RAG. ``parent_content`` is JOIN-fetched
    # at retrieval time when the matched leaf carries a
    # ``parent_chunk_id``; downstream RAG agents can prefer it over
    # ``content`` when assembling LLM context for richer structural
    # framing. Always None for legacy rows / root-level chunks.
    parent_chunk_id: int | None = None
    parent_content: str | None = None
    chunk_type: str = "leaf"
    chunk_level: int = 0


class SearchResponse(BaseModel):
    query: str
    embedding_model: str
    embedding_dim: int
    results: list[SearchHitOut]


class ImageSearchRequest(BaseModel):
    """Studio-extraction (Phase 1) image search request.

    Defaults differ from text ``SearchRequest`` (top_k=8, min_score=0)
    because the consumer (anila-studio) hydrates a deck-level image
    candidate set and wants a slightly broader sweep than chat hits.
    """

    query: str = Field(..., min_length=1, max_length=4000, description="自然語言查詢")
    top_k: int = Field(
        default=8,
        ge=1,
        le=50,
        description="回傳前 N 張最相似的圖片",
    )
    min_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="cosine 相似度最低門檻 (0=不過濾)",
    )


class ImageHitOut(BaseModel):
    """One image hit with hydration metadata. ``image_id`` is the
    ``ingestion_images.id`` (BIGSERIAL PK) — globally unique and
    addressable via ``GET /api/ingestion/images/{image_id}/blob``.
    """

    image_id: int
    document_id: int
    page: int | None
    storage_path: str
    mime: str
    caption: str | None
    filename: str
    score: float = Field(..., description="cosine similarity in [0, 1]; higher = closer")


class ImageSearchResponse(BaseModel):
    query: str
    embedding_model: str
    embedding_dim: int
    results: list[ImageHitOut]


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
    principal: SearchPrincipal = Depends(resolve_search_principal),
) -> SearchResponse:
    """Semantic top-K retrieval over one collection's chunks.

    Auth: ``_require_collection_access`` — admin or owner. An agent csk-
    authenticates as its owner but is additionally hard-scoped to its single
    ``bound_collection_id`` (S-Q1). Cross-user sharing is a future
    ``collection_access_grants`` feature.
    """
    _enforce_agent_collection_scope(principal, collection_id)
    current_user = principal.user
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
                # Sprint 9 X — surface parent context if the storage
                # layer attached it (CollectionScopedPgVectorStore
                # populates ``parent_content`` via _attach_parent_content
                # in the same round-trip after the vector match).
                parent_chunk_id=getattr(h.chunk, "parent_chunk_id", None),
                parent_content=getattr(h, "parent_content", None),
                chunk_type=getattr(h.chunk, "chunk_type", "leaf") or "leaf",
                chunk_level=getattr(h.chunk, "chunk_level", 0) or 0,
            )
            for h in hits
        ],
    )


# ── Image search ────────────────────────────────────────────────────────────


@router.post(
    "/api/ingestion/collections/{collection_id}/images/search",
    response_model=ImageSearchResponse,
)
async def search_collection_images(
    collection_id: int,
    payload: ImageSearchRequest,
    db: Session = Depends(get_db),
    principal: SearchPrincipal = Depends(resolve_search_principal),
) -> ImageSearchResponse:
    """Semantic top-K retrieval over a collection's ``ingestion_images`` rows.

    Phase 1 of the anila-studio extraction: this endpoint replaces the
    in-process ``app.api.studio._retrieve_images`` helper so the studio
    sub-service can pull image hits over HTTP instead of binding to the
    csp-db pgvector layer directly.

    Auth: same as chunk search — admin OR collection owner. Future
    sharing flows (collection_access_grants) plug into
    ``_require_collection_access``.

    Returns ``results=[]`` (not an error) when:
      - the collection has zero indexed images (text-only KB),
      - the embedder returned an empty vector,
      - no rows beat the ``min_score`` threshold.
    """
    _enforce_agent_collection_scope(principal, collection_id)
    current_user = principal.user
    coll = _require_collection_access(db, current_user, collection_id)

    if coll.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Collection {collection_id} is {coll.status}; reactivate before search.",
        )

    q_vec = await _embed_query(
        db,
        current_user,
        coll.embedding_model,
        coll.embedding_dim,
        payload.query,
    )
    if not q_vec:
        return ImageSearchResponse(
            query=payload.query,
            embedding_model=coll.embedding_model,
            embedding_dim=coll.embedding_dim,
            results=[],
        )

    try:
        pool = get_pool()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    # Wrap with HalfVector — the same codec PgPool registers on every
    # connection. Passing a Python string + ::halfvec cast fails because
    # halfvec's text-input parser misreads the leading `[`. HalfVector
    # ships the right binary wire format directly. Mirrors the path in
    # ``studio._retrieve_images``.
    from pgvector import HalfVector

    q_value = HalfVector(q_vec)

    # halfvec uses cosine distance; pgvector returns 0 = identical, so
    # similarity = 1 - distance. Filter on distance < (1 - min_score).
    max_dist = 1.0 - payload.min_score
    async with pool.acquire() as conn, conn.transaction():
        # RLS: ingestion_images is FORCE-RLS (migration 0037); scope this
        # connection to the collection so the policy returns its rows. SET LOCAL
        # is txn-scoped, so it never leaks to the next pooled user. The explicit
        # WHERE i.collection_id = $1 below stays as belt-and-suspenders.
        await conn.execute(f"SET LOCAL anila.collection_id = {int(collection_id)}")
        rows = await conn.fetch(
            """
            SELECT
                i.id AS pk_id,
                i.image_id,
                i.document_id,
                i.page,
                i.storage_path,
                i.mime,
                i.caption,
                d.filename,
                (i.embedding <=> $2) AS dist
            FROM ingestion_images i
            JOIN ingestion_documents d ON d.id = i.document_id
            WHERE i.collection_id = $1
              AND i.embedding IS NOT NULL
              AND (i.embedding <=> $2) < $3
            ORDER BY i.embedding <=> $2
            LIMIT $4
            """,
            collection_id, q_value, max_dist, payload.top_k,
        )

    return ImageSearchResponse(
        query=payload.query,
        embedding_model=coll.embedding_model,
        embedding_dim=coll.embedding_dim,
        results=[
            ImageHitOut(
                # Expose the table PK (BIGSERIAL) as image_id so the
                # blob endpoint can address rows uniquely. The TEXT
                # ``image_id`` column is only unique per document and
                # is not surfaced — callers should not rely on it.
                image_id=int(r["pk_id"]),
                document_id=int(r["document_id"]),
                page=r["page"],
                storage_path=str(r["storage_path"]),
                mime=str(r["mime"]),
                caption=(r["caption"] or None),
                filename=str(r["filename"]),
                score=float(1.0 - r["dist"]),
            )
            for r in rows
        ],
    )
