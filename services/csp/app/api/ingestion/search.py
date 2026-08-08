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

import logging
from types import SimpleNamespace
from typing import Any

from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from anila_core.storage.adapters.pgvector_store import (
    CollectionScopedPgVectorStore,
)

from app.api.ingestion.collections import _require_collection_access
from app.database import get_db
from app.models.ingestion import DocumentRelation, IngestionDocument
from app.models.model_registry import ModelRegistry
from app.models.user import User
from app.services.auth_service import get_current_user
from app.services.ingestion_pool import get_pool
from app.services.proxy_service import downstream_identity, proxy_request
from app.services.endpoint_author_service import visible_endpoint_url
from app.services.relation_resolver import scope_collection_rls
from app.services.search_expansion import expand_query

router = APIRouter(tags=["Ingestion / Search"])
logger = logging.getLogger(__name__)

_search_bearer = HTTPBearer(auto_error=False)


@dataclass
class SearchPrincipal:
    """Effective principal for a search request.

    ``user`` is the identity we authorise against. For an in-task agent
    callback (dispatch JWT) this is still the agent's OWNER — that mapping
    is load-bearing for ``_require_collection_access`` / collection RLS
    (bound collections are owned by the agent owner; swapping in the JWT's
    real ``user_id`` would either 403 legitimate bound searches or widen
    access). ``agent`` is set only for the dispatch-JWT path; the endpoint
    then hard-scopes to the agent's bound collection set (P4.7 / S-Q1).
    """

    user: User
    agent: "object | None" = None


def _principal_from_dispatch_claims(db: Session, claims: dict) -> SearchPrincipal:
    """Map verified dispatch claims → SearchPrincipal (owner + agent)."""
    from app.models.agent import Agent

    agent_id = int(claims["agent_id"])
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if agent is None:
        raise HTTPException(status_code=401, detail="dispatch token 對應的 agent 不存在")
    owner = db.query(User).filter(User.id == agent.owner_user_id).first()
    if owner is None:
        raise HTTPException(status_code=401, detail="agent owner 不存在")
    return SearchPrincipal(user=owner, agent=agent)


def resolve_search_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_search_bearer),
    db: Session = Depends(get_db),
) -> SearchPrincipal:
    """Search auth: user (JWT/sk-/cookie) OR in-task agent dispatch JWT.

    P2.1 W2: agent callbacks present the same 5-minute RS256 dispatch JWT
    CSP minted for the task (``Authorization: Bearer <jwt>``). Bare ``csk-``
    strings are rejected — zero agents hold static credentials today.
    """
    from jose import JWTError, jwt as jose_jwt

    from app.services.proxy.dispatch_token import (
        DISPATCH_TOKEN_AUDIENCE,
        verify_dispatch_token,
    )

    token = credentials.credentials if (credentials and credentials.credentials) else None
    if token:
        # Retired static agent credential — fail closed, no dual-accept.
        if token.startswith("csk-"):
            raise HTTPException(
                status_code=401,
                detail="agent 任務回呼請使用派工 JWT，不再接受 csk-",
            )
        claims = verify_dispatch_token(token)
        if claims is not None:
            return _principal_from_dispatch_claims(db, claims)
        # Dispatch-shaped but invalid/expired/tampered → 401 (do not fall
        # through to the user-JWT resolver and muddy the error).
        try:
            unverified = jose_jwt.get_unverified_claims(token)
        except JWTError:
            unverified = None
        if unverified is not None:
            aud = unverified.get("aud")
            is_dispatch_aud = (
                DISPATCH_TOKEN_AUDIENCE in aud
                if isinstance(aud, list)
                else aud == DISPATCH_TOKEN_AUDIENCE
            )
            if is_dispatch_aud:
                raise HTTPException(
                    status_code=401, detail="派工 JWT 無效或已過期"
                )

    # User path — delegate to the existing resolver (Authorization header / sk- /
    # httpOnly cookie). Raises 401 when no valid user credential is present.
    user = get_current_user(request, credentials, db)
    return SearchPrincipal(user=user, agent=None)


def _enforce_agent_collection_scope(principal: SearchPrincipal, collection_id: int) -> None:
    """For the agent dispatch-JWT path, reject any collection outside the
    agent's bound set (P4.7). Empty set = non-RAG agent → every collection
    is off-limits. No-op for user principals."""
    agent = principal.agent
    if agent is None:
        return
    from app.services.agent_collection_bindings import get_bound_collection_ids

    allowed = get_bound_collection_ids(agent)
    if collection_id not in allowed:
        raise HTTPException(
            status_code=403,
            detail="此 agent 的憑證無權搜尋該 collection（僅限其綁定的知識庫）",
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
        description=(
            "cosine 相似度最低門檻 (0=不過濾)。"
            "⚠ 不要把它讀成「相似百分比」,也不要沿用 0.7 之類看起來合理的預設值:"
            "本平台的 embedder 是非對稱的 —— 查詢側與文件側對「同一句話」的編碼,"
            "實測餘弦值約在 0.60–0.83 之間,不會是 1.0。"
            "0.7 剛好壓在「同一句話」的分數帶上方,設下去會把所有段落永遠濾光,"
            "而且看起來像是知識庫沒有資料。"
            "目前平台上線的門檻沒有高於 0.3 的;要設門檻請先以自己的語料實測。"
        ),
    )
    document_ids: list[int] | None = Field(
        default=None,
        description=(
            "可選：僅在指定 documents 內檢索（None=全 collection）。"
            "前端如要做 'in this doc' 之類的範圍縮限會用到。"
        ),
    )
    # ── Cross-document relation expansion (design v2 §7) — all opt-in, B/C ──────
    expand_relations: bool = Field(
        default=False,
        description=(
            "沿 document_relations 邊做 1-hop 展開：主 top-k 命中的文件若有關聯"
            "(母法/補充/修正…),把關聯文件的代表片段一併帶回 `related`。"
            "預設關閉,向後相容。"
        ),
    )
    relation_types: list[str] | None = Field(
        default=None,
        description="僅展開這些 relation_type(None=全部);如 ['based_on','supplements']。",
    )
    max_related: int = Field(
        default=5,
        ge=0,
        le=50,
        description="`related` 最多回傳幾份關聯文件(0=不展開)。",
    )
    min_relation_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="只展開 confidence ≥ 此值的邊(rule=1.0、llm 可能較低)。",
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


class RelatedHit(BaseModel):
    """A document reached by a 1-hop relation edge from a main top-k hit
    (design v2 §7). Additive context for the RAG agent — never replaces the
    primary ``results``."""

    document_id: int
    filename: str
    title: str | None = None
    relation_type: str = Field(..., description="based_on / amends / supersedes / cites / supplements / relates")
    target_ref: str = Field(..., description="the cited regulation name [+article] on the edge")
    source: str = Field(..., description="edge author: rule / manual / llm")
    direction: str = Field(
        ...,
        description=(
            "'outgoing' = a main-hit document points AT this one (e.g. cites it); "
            "'incoming' = this document points at a main-hit document."
        ),
    )
    via_document_id: int = Field(..., description="the main-hit document on the other end of the edge")
    confidence: float = 1.0
    # representative chunk from the related document (best match for the query;
    # None if the related doc has no indexed leaf chunk yet)
    chunk_id: int | None = None
    chunk_key: str | None = None
    content: str | None = None
    score: float | None = None


class SearchResponse(BaseModel):
    query: str
    embedding_model: str
    embedding_dim: int
    results: list[SearchHitOut]
    related: list[RelatedHit] = Field(
        default_factory=list,
        description="1-hop relation expansion (empty unless expand_relations=True).",
    )


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

    from anila_core.memory.long_term import EMBED_DIM

    native = getattr(model, "embedding_native_dim", None)
    if isinstance(native, int) and native > 0 and native != EMBED_DIM:
        pad_from: int | None = native
    else:
        pad_from = None

    # Snapshot every attribute proxy_request (and ceiling helpers) may read into
    # a plain namespace. ORM instances are subject to expire_on_commit; a bare
    # attribute touch before commit is NOT a load guarantee under either
    # posture. The snapshot is the guarantee: after db.commit() releases the
    # pooled connection, proxy_request must not re-checkout via lazy reload.
    model_snapshot = SimpleNamespace(
        id=model.id,
        name=model.name,
        model_type=model.model_type,
        endpoint_url=model.endpoint_url,
        api_version=model.api_version,
        protocol=getattr(model, "protocol", None) or "openai_compatible",
        api_key_secret_ref=model.api_key_secret_ref,
        classification_ceiling=model.classification_ceiling,
        is_active=model.is_active,
        display_name=getattr(model, "display_name", model.name),
        is_internal=bool(getattr(model, "is_internal", False)),
    )
    user_id = user.id
    department_id = user.department_id
    identity = downstream_identity(user)
    api_version = model_snapshot.api_version if model_snapshot.api_version in ("v1", "v2") else "v1"
    db.commit()

    body = {"model": model_name, "input": query}
    response = await proxy_request(
        model=model_snapshot,
        api_key_id=None,  # SPA caller; usage attributes to user, no key
        user_id=user_id,
        user_identity=identity,
        department_id=department_id,
        request_body=body,
        endpoint_path=f"/{api_version}/embeddings",
        endpoint_display=visible_endpoint_url(
            model_snapshot.endpoint_url,
            is_internal=model_snapshot.is_internal,
            db=db,
            caller=user,
        ),
        # Query-side: must not silently fall through to Triton's documents input.
        embedding_input_role="query",
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

    from anila_core.memory.long_term import EMBED_DIM, truncate_embedding

    try:
        floats = [float(x) for x in raw_vector]
        if embedding_dim == EMBED_DIM:
            return truncate_embedding(floats, pad_from=pad_from)
        # Non-production column widths (unit tests use small dims): keep
        # the historical truncate-or-422 contract so fixtures stay cheap.
        if len(floats) < embedding_dim:
            raise ValueError(
                f"dim {len(floats)} < collection embedding_dim {embedding_dim}"
            )
        return floats[:embedding_dim]
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Model returned {len(raw_vector)}-d vector but collection's "
                f"chunks are stored as {embedding_dim}-d. The collection was "
                "indexed against a different model — reindex before searching."
            ),
        ) from e


async def _expand_relations(
    db: Session,
    store: "CollectionScopedPgVectorStore",
    *,
    collection_id: int,
    main_doc_ids: set[int],
    query_vec: list[float],
    payload: "SearchRequest",
) -> list[RelatedHit]:
    """1-hop relation expansion over ``document_relations`` (design v2 §7).

    For the set ``D`` of documents the main top-k hit, find resolved edges with
    one end in ``D`` (``relation_type`` / ``confidence`` filtered), take the
    OTHER end as a related document, and fetch its single best chunk for the
    query via ``similarity_search_per_document`` (one batched query, not N
    scans). Highest-confidence edges win the ``max_related`` cap.
    """
    if not main_doc_ids or payload.max_related <= 0:
        return []

    # csp_app + FORCE-RLS: scope this transaction so the policy yields rows.
    scope_collection_rls(db, collection_id)

    q = (
        db.query(DocumentRelation)
        .filter(
            DocumentRelation.collection_id == collection_id,
            DocumentRelation.dst_document_id.isnot(None),  # only resolved edges expand
            DocumentRelation.confidence >= payload.min_relation_confidence,
            or_(
                DocumentRelation.src_document_id.in_(main_doc_ids),
                DocumentRelation.dst_document_id.in_(main_doc_ids),
            ),
        )
        .order_by(DocumentRelation.confidence.desc(), DocumentRelation.id)
    )
    if payload.relation_types:
        q = q.filter(DocumentRelation.relation_type.in_(payload.relation_types))

    # First edge per related document wins (already confidence-ordered). Cap.
    picked: dict[int, DocumentRelation] = {}
    direction: dict[int, str] = {}
    via: dict[int, int] = {}
    for e in q.all():
        if e.src_document_id in main_doc_ids and e.dst_document_id not in main_doc_ids:
            rel_doc, edge_dir, edge_via = e.dst_document_id, "outgoing", e.src_document_id
        elif e.dst_document_id in main_doc_ids and e.src_document_id not in main_doc_ids:
            rel_doc, edge_dir, edge_via = e.src_document_id, "incoming", e.dst_document_id
        elif e.src_document_id in main_doc_ids and e.dst_document_id in main_doc_ids:
            # both ends already surfaced by the main search — still note the link
            rel_doc, edge_dir, edge_via = e.dst_document_id, "outgoing", e.src_document_id
        else:
            continue
        if rel_doc in picked:
            continue
        picked[rel_doc] = e
        direction[rel_doc] = edge_dir
        via[rel_doc] = edge_via
        if len(picked) >= payload.max_related:
            break

    related_doc_ids = list(picked)
    if not related_doc_ids:
        return []

    # Snapshot edge attrs into plain namespaces before releasing the pool —
    # bare attribute-touch is NOT a load guarantee (same conclusion as
    # _embed_query). After commit, use the snapshot only.
    edge_snapshots = {
        rel_doc: SimpleNamespace(
            relation_type=e.relation_type,
            target_ref=e.target_ref,
            source=e.source,
            confidence=e.confidence,
        )
        for rel_doc, e in picked.items()
    }
    db.commit()

    # One representative chunk per related document (batched, RANK-partitioned).
    rep: dict[int, Any] = {}
    for h in await store.similarity_search_per_document(
        query_embedding=query_vec, document_ids=related_doc_ids, k=1, min_score=0.0
    ):
        rep.setdefault(h.chunk.document_id, h)

    # Fresh checkout: re-apply SET LOCAL RLS scope (bound to the new txn).
    scope_collection_rls(db, collection_id)
    meta_rows = (
        db.query(
            IngestionDocument.id, IngestionDocument.filename, IngestionDocument.title
        )
        .filter(IngestionDocument.id.in_(related_doc_ids))
        .all()
    )
    meta = {r.id: (r.filename, r.title) for r in meta_rows}

    out: list[RelatedHit] = []
    for rel_doc in related_doc_ids:
        e = edge_snapshots[rel_doc]
        fn, title = meta.get(rel_doc, ("<unknown>", None))
        h = rep.get(rel_doc)
        out.append(
            RelatedHit(
                document_id=rel_doc,
                filename=fn,
                title=title,
                relation_type=e.relation_type,
                target_ref=e.target_ref,
                source=e.source,
                direction=direction[rel_doc],
                via_document_id=via[rel_doc],
                confidence=e.confidence,
                chunk_id=h.chunk.id if h else None,
                chunk_key=h.chunk.chunk_key if h else None,
                content=h.chunk.content if h else None,
                score=h.score if h else None,
            )
        )
    return out


async def _assert_index_matches_designation(
    store: "CollectionScopedPgVectorStore",
    *,
    hits: list,
    designated: str | None,
    collection_id: int,
) -> None:
    """Refuse to answer "nothing" when the corpus is indexed under another model.

    ``similarity_search`` filters on ``embedding_source_model`` (P4.8).
    Designating a different platform embedding is a supported admin
    action, and the instant it happens every existing chunk stops
    matching: the endpoint keeps returning ``200 {"results": []}``,
    forever, with no error and no log line. A knowledge base full of
    documents becomes a knowledge base that answers nothing, and nothing
    anywhere says why. That is the failure mode this guard exists for —
    silent success is more dangerous than an error.

    Fires only when the vector layer itself returned nothing AND a
    designation filter was in play. A legitimately empty result — empty
    collection, or a query that genuinely matched no passage — takes the
    early return and the response is byte-for-byte what it was before.

    Three outcomes:

    * no rows under any other model → genuinely empty; say nothing.
    * rows under both the designated and another model → the collection
      is mid-reindex, search still works on the matching half. Log for
      the operator; the caller's result is unchanged. ``has_matching``
      is an exact ``EXISTS``, so a deliberate mixed-model corpus during
      a migration lands here every time and is never refused.
    * rows only under another model → the whole collection is stranded.
      409, same code this endpoint already uses for "the collection is
      not in a searchable state" (archived collections, above).

    This is the only hard failure in the package, so it is held to the
    owner's three questions:

    ① Who gets blocked? Only a caller whose collection holds zero
      retrievable chunks — every such call previously returned an empty
      list, so no call that could have returned results can reach the
      raise. The early return above is the guarantee: ``hits`` non-empty
      exits first, and the check runs before the ``document_ids``
      post-filter so a narrowed search cannot be misread.
    ② How do they rescue themselves? Only remedies that were executed
      end to end are named. The operator re-designates the previous
      embedding model — instant, non-destructive, and it is the one that
      actually fixes the whole platform at once. The user's own option is
      DELETE the document then upload it again. Two routes that look
      obvious were checked and do NOT work, so the message warns against
      one and never mentions the other: plain re-upload hits
      ``uq_documents_collection_sha256``, whose ``IntegrityError`` handler
      returns the existing row *above* the ``enqueue_ingest_document``
      call (``documents.py``), so no job is created; and ``/reprocess``
      accepts only ``status='failed'`` while a stranded document is
      ``indexed``. There is no reindex capability on this platform —
      do not write one into a user-facing message.
    ③ Will it one day block us? A mixed-model migration cannot trigger
      it (see above — that is why ``has_matching`` is exact rather than
      sampled). Callers of this endpoint on a 409, each checked rather
      than assumed: ``anila-studio`` handles it everywhere —
      ``report_runner`` catches ``CspClientError`` explicitly,
      ``datatables`` / ``infographics`` / ``studio`` swallow it via
      ``except Exception`` and fall through to no-context mode, and
      ``mindmaps`` surfaces it as a failed job carrying the message.
      ``anila_agent.retrieval.csp_http`` calls ``raise_for_status()``,
      so an in-task agent sees an exception rather than empty context.
      ``infra/loadtest/profile-search.js`` hard-fails on any non-200, so
      the load-test profile will fail against a stranded corpus instead
      of reporting empty results — worth knowing before someone reads a
      red load test as a regression.

    The end-user detail names the designated model (already readable by
    any authenticated caller via ``GET /api/models/platform-embedding``)
    and nothing else. Which model the stale rows carry, and how the
    collection got there, go to the operator log only.

    Scope — do NOT read this as "the platform no longer returns a silent
    empty". It covers **chunk search on this endpoint only**. Image
    search (``search_collection_images`` below) and memory retrieval
    (``app/services/memory_service.py``) apply the same
    ``embedding_source_model`` filter and still return an empty result
    with nothing said — by decision, not oversight. A designation change
    strands those paths exactly as it strands this one.

    ``coverage`` describes a bounded sample, not the whole collection;
    ``source_model_coverage`` documents what that sampling can miss.
    """
    if hits or designated is None:
        return

    coverage = await store.source_model_coverage(designated)
    if not coverage.has_other:
        return

    if coverage.has_matching:
        logger.warning(
            "collection %s is partially indexed: some chunks are under the "
            "designated embedding %r and some under %r. Search still works "
            "over the matching half; the rest stays unreachable until those "
            "documents are deleted and re-uploaded, or the designation is "
            "put back (there is no reindex capability on this platform).",
            collection_id,
            designated,
            coverage.sample_other_model,
        )
        return

    logger.error(
        "collection %s has no chunks under the designated embedding %r — "
        "every indexed chunk carries %r instead. Search is answering 409 "
        "rather than an empty result set. Restore by designating (and if "
        "necessary reactivating) the model those chunks carry; there is no "
        "reindex capability on this platform.",
        collection_id,
        designated,
        coverage.sample_other_model,
    )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            "這個知識庫目前檢索不到任何內容。原因不是沒有資料——文件都還在,"
            f"是既有文件的索引由另一個向量模型建立,與平台現在指定的"
            f"「{designated}」對不起來。"
            "這要由平台管理員處理:把這些文件當初使用的 embedding 模型"
            "重新指定為平台主 embedding;若那個模型已被停用,要先重新啟用再指定"
            "(停用中的模型不能指定,系統會直接拒絕)。"
            "受影響的知識庫會立刻恢復,而且不會動到任何資料。"
            "請聯絡管理員,並附上這個知識庫的名稱。"
            "你自己唯一能做的是把急著要用的文件「先刪除、再重新上傳」,"
            "一次一份;刪掉的文件與其處理歷程不會回來。"
            "直接重傳同一個檔沒有用——系統會判定為重複,不會重建索引。"
        ),
    )


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

    Auth: ``_require_collection_access`` — admin or owner. An agent
    dispatch JWT authenticates as its owner but is additionally
    hard-scoped to its bound collection set (P4.7 / S-Q1). Cross-user
    sharing is a future ``collection_access_grants`` feature.
    """
    _enforce_agent_collection_scope(principal, collection_id)
    current_user = principal.user
    coll = _require_collection_access(db, current_user, collection_id)

    if coll.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Collection {collection_id} is {coll.status}; reactivate before search.",
        )

    # P4.8: query + filter both use the designated platform embedding when
    # set, so the query lives in the same semantic space as retrieved rows.
    from app.services.platform_embedding import resolve_platform_embedding

    designated = resolve_platform_embedding(db)
    embed_model = designated.name if designated is not None else coll.embedding_model
    source_filter = designated.name if designated is not None else None

    # 民國紀年／域內同義擴展後再 embedding（擴展詞會拉近向量空間，屬預期行為）。
    search_query = expand_query(db, payload.query)
    if search_query != payload.query:
        added_n = len(search_query[len(payload.query) :].split())
        logger.debug(
            "query expansion applied: original_len=%d added_terms=%d",
            len(payload.query),
            added_n,
        )

    query_vec = await _embed_query(
        db,
        current_user,
        embed_model,
        coll.embedding_dim,
        search_query,
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
        source_model=source_filter,
    )

    # Zero hits under a designation filter is ambiguous — resolve it before
    # the caller can read the emptiness as "this knowledge base has nothing
    # to say". Runs before the document_ids post-filter below so a caller
    # narrowing to one document never gets misdiagnosed as a stranded index.
    await _assert_index_matches_designation(store, hits=hits, designated=source_filter, collection_id=coll.id)

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

    related: list[RelatedHit] = []
    if payload.expand_relations:
        related = await _expand_relations(
            db,
            store,
            collection_id=coll.id,
            main_doc_ids=doc_ids,
            query_vec=query_vec,
            payload=payload,
        )

    return SearchResponse(
        query=payload.query,
        embedding_model=coll.embedding_model,
        embedding_dim=coll.embedding_dim,
        related=related,
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

    from app.services.platform_embedding import resolve_platform_embedding

    designated = resolve_platform_embedding(db)
    embed_model = designated.name if designated is not None else coll.embedding_model
    source_filter = designated.name if designated is not None else None

    q_vec = await _embed_query(
        db,
        current_user,
        embed_model,
        coll.embedding_dim,
        payload.query,
    )
    if not q_vec:
        return ImageSearchResponse(
            query=payload.query,
            embedding_model=embed_model,
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
        if source_filter is not None:
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
                  AND lower(i.embedding_source_model) = lower($5)
                  AND (i.embedding <=> $2) < $3
                ORDER BY i.embedding <=> $2
                LIMIT $4
                """,
                collection_id, q_value, max_dist, payload.top_k, source_filter,
            )
        else:
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
        embedding_model=embed_model,
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
