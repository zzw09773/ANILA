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

from app.api.ingestion.surface import (
    CollectionOrigin,
    OriginArg,
    require_surface_origin,
    surface_origin_dep,
)

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_
from sqlalchemy.orm import Session

from anila_core.storage.adapters.pgvector_store import (
    CollectionScopedPgVectorStore,
)
from anila_contracts import Classification

from app.database import get_db
from app.models.ingestion import (
    DocumentRelation,
    IngestionCollection,
    IngestionDocument,
)
from app.models.user import User
from app.modules.clearance.service import (
    ClearancePolicyDataError,
    DataAccessDecision,
    resolve_and_evaluate_data_access,
)
from app.services.auth_service import get_current_user
from app.services.ingestion_pool import get_pool
from app.services.inference_audit import (
    record_at_acceptance,
    record_at_outcome,
    record_inference_audit,
    short_audit_reason,
)
from app.services.relation_resolver import scope_collection_rls
from app.services.retrieval_service import RetrievalFailure, embed_query

router = APIRouter(tags=["Ingestion / Search"])

_search_bearer = HTTPBearer(auto_error=False)


@dataclass
class SearchPrincipal:
    """Effective principal for a search request.

    ``user`` is the identity we authorise against (for an agent csk- it is the
    agent's OWNER). ``agent`` is set only when the caller authenticated with an
    agent service token; the endpoint then hard-scopes it to
    ``agent.bound_collection_id`` (S-Q1, least privilege).

    ``skip_inference_audit`` suppresses the end-user inference audit row
    (Studio runtime / other service hops that re-enter this function).
    """

    user: User
    agent: "object | None" = None
    skip_inference_audit: bool = False


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


def _agent_classification_ceiling(
    principal: SearchPrincipal,
) -> Classification | None:
    if principal.agent is None:
        return None
    raw = getattr(principal.agent, "classification_ceiling", None)
    if not isinstance(raw, str):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="agent classification ceiling 缺失，已拒絕檢索",
        )
    try:
        return Classification.from_storage(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="agent classification ceiling 無效，已拒絕檢索",
        ) from exc


def _resolved_data_access(
    db: Session,
    *,
    principal: SearchPrincipal,
    collection_id: int,
    document_id: int | None = None,
) -> tuple[DataAccessDecision, Classification | None] | None:
    try:
        decision = resolve_and_evaluate_data_access(
            db,
            user_id=principal.user.id,
            collection_id=collection_id,
            document_id=document_id,
        )
    except LookupError:
        return None
    except ClearancePolicyDataError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="clearance policy data 無效，已 fail-closed",
        ) from exc
    if not decision.allowed:
        return None
    if decision.authorized_classification is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="clearance decision 缺少 authorized classification",
        )
    ceiling = _agent_classification_ceiling(principal)
    if ceiling is not None and decision.context.required_classification > ceiling:
        return None
    return decision, ceiling


def _query_classification_ceiling(
    decision: DataAccessDecision,
    agent_ceiling: Classification | None,
) -> Classification:
    authorized = decision.authorized_classification
    if authorized is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="clearance decision 缺少 authorized classification",
        )
    if agent_ceiling is None or authorized <= agent_ceiling:
        return authorized
    return agent_ceiling


def _data_access_ceiling(
    db: Session,
    *,
    principal: SearchPrincipal,
    collection_id: int,
    document_id: int | None = None,
) -> Classification | None:
    resolved = _resolved_data_access(
        db,
        principal=principal,
        collection_id=collection_id,
        document_id=document_id,
    )
    if resolved is None:
        return None
    return _query_classification_ceiling(*resolved)


def _is_data_access_allowed(
    db: Session,
    *,
    principal: SearchPrincipal,
    collection_id: int,
    document_id: int | None = None,
) -> bool:
    return _data_access_ceiling(
        db,
        principal=principal,
        collection_id=collection_id,
        document_id=document_id,
    ) is not None


def _require_collection_clearance(
    db: Session,
    *,
    principal: SearchPrincipal,
    collection_id: int,
    origin: OriginArg,
) -> IngestionCollection:
    """Resolve collection for search with an explicit surface decision.

    Dual-mounted search routes pass ``origin=require_surface_origin()``.
    Studio runtime (cross-product, already bound via Task/Snapshot) passes
    ``origin=ANY_SURFACE``.
    """
    from app.api.ingestion.collections import lookup_collection_for_surface

    collection = lookup_collection_for_surface(
        db, collection_id, origin=origin
    )
    if collection is None:
        raise HTTPException(status_code=404, detail="Collection not found")
    if not _is_data_access_allowed(
        db,
        principal=principal,
        collection_id=collection_id,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="clearance/compartment/need-to-know/collection grant 不足",
        )
    return collection


@dataclass(frozen=True, slots=True)
class _AuthorizedDocumentAccess:
    query_ceiling: Classification
    required_classification: Classification


def _authorized_document_access(
    db: Session,
    *,
    principal: SearchPrincipal,
    collection_id: int,
    requested_ids: list[int] | None,
    reject_denied: bool,
) -> dict[int, _AuthorizedDocumentAccess]:
    query = db.query(IngestionDocument.id).filter(
        IngestionDocument.collection_id == collection_id
    )
    if requested_ids is not None:
        query = query.filter(IngestionDocument.id.in_(requested_ids))
    found = sorted(int(row.id) for row in query.all())
    if requested_ids is not None and set(found) != set(requested_ids):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="document_ids 含不存在或不屬於此 collection 的文件",
        )

    allowed: dict[int, _AuthorizedDocumentAccess] = {}
    for document_id in found:
        resolved = _resolved_data_access(
            db,
            principal=principal,
            collection_id=collection_id,
            document_id=document_id,
        )
        if resolved is not None:
            decision, agent_ceiling = resolved
            allowed[document_id] = _AuthorizedDocumentAccess(
                query_ceiling=_query_classification_ceiling(
                    decision, agent_ceiling
                ),
                required_classification=decision.context.required_classification,
            )
    if reject_denied and len(allowed) != len(found):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="至少一份 document 的 clearance/compartment 不足",
        )
    return allowed


def _authorized_document_ceilings(
    db: Session,
    *,
    principal: SearchPrincipal,
    collection_id: int,
    requested_ids: list[int] | None,
    reject_denied: bool,
) -> dict[int, Classification]:
    return {
        document_id: access.query_ceiling
        for document_id, access in _authorized_document_access(
            db,
            principal=principal,
            collection_id=collection_id,
            requested_ids=requested_ids,
            reject_denied=reject_denied,
        ).items()
    }


def _authorized_document_ids(
    db: Session,
    *,
    principal: SearchPrincipal,
    collection_id: int,
    requested_ids: list[int] | None,
    reject_denied: bool,
) -> list[int]:
    return list(
        _authorized_document_ceilings(
            db,
            principal=principal,
            collection_id=collection_id,
            requested_ids=requested_ids,
            reject_denied=reject_denied,
        )
    )


def _documents_by_ceiling(
    ceilings: dict[int, Classification],
) -> dict[Classification, list[int]]:
    grouped: dict[Classification, list[int]] = {}
    for document_id, ceiling in ceilings.items():
        grouped.setdefault(ceiling, []).append(document_id)
    return grouped


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
        min_length=1,
        max_length=100,
        description=(
            "可選：僅在指定 documents 內檢索（None=全 collection）。"
            "前端如要做 'in this doc' 之類的範圍縮限會用到。"
        ),
    )

    @field_validator("document_ids")
    @classmethod
    def _document_ids_are_positive_and_unique(
        cls, value: list[int] | None
    ) -> list[int] | None:
        if value is None:
            return None
        if any(not isinstance(document_id, int) or isinstance(document_id, bool)
               or document_id <= 0 for document_id in value):
            raise ValueError("document_ids 必須全部是正整數")
        if len(value) != len(set(value)):
            raise ValueError("document_ids 不允許重複")
        return value
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
    embedding_fingerprint: str
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
    document_ids: list[int] | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="選填；先在 SQL 限縮到 canonical snapshot 文件",
    )

    @field_validator("document_ids")
    @classmethod
    def _validate_document_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        if any(type(item) is not int or item <= 0 for item in value):
            raise ValueError("document_ids 必須是正整數陣列")
        if len(value) != len(set(value)):
            raise ValueError("document_ids 不得重複")
        return value


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
    embedding_fingerprint: str
    embedding_dim: int
    results: list[ImageHitOut]


# ── Helpers ─────────────────────────────────────────────────────────────────


async def _embed_query(
    db: Session,
    user: User,
    model_name: str,
    embedding_dim: int,
    query: str,
    *,
    trusted_classification_level: Classification,
) -> list[float]:
    try:
        return await embed_query(
            db,
            user,
            model_name,
            embedding_dim,
            query,
            inference_callsite_id="csp.standalone_search_embedding",
            trusted_classification_level=trusted_classification_level,
        )
    except RetrievalFailure as exc:
        raise HTTPException(
            status_code=(
                status.HTTP_403_FORBIDDEN
                if exc.code == "embedding_policy_denied"
                else status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=f"{exc.code}: {exc}",
        ) from exc


async def _expand_relations(
    db: Session,
    store: "CollectionScopedPgVectorStore",
    *,
    principal: SearchPrincipal,
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
        # A caller-provided document scope is a hard retrieval boundary, not
        # merely a seed set.  Relation expansion may never escape it.
        if payload.document_ids is not None and rel_doc not in set(payload.document_ids):
            continue
        picked[rel_doc] = e
        direction[rel_doc] = edge_dir
        via[rel_doc] = edge_via
        if len(picked) >= payload.max_related:
            break

    related_doc_ids = list(picked)
    if not related_doc_ids:
        return []

    authorized = _authorized_document_ceilings(
        db,
        principal=principal,
        collection_id=collection_id,
        requested_ids=related_doc_ids,
        reject_denied=False,
    )
    related_doc_ids = [
        document_id for document_id in related_doc_ids if document_id in authorized
    ]
    if not related_doc_ids:
        return []

    # One representative chunk per related document (batched, RANK-partitioned).
    rep: dict[int, Any] = {}
    for ceiling, document_ids in _documents_by_ceiling(authorized).items():
        for hit in await store.similarity_search_per_document_authorized(
            query_embedding=query_vec,
            document_ids=document_ids,
            classification_ceiling=ceiling,
            k=1,
            min_score=0.0,
        ):
            rep.setdefault(hit.chunk.document_id, hit)

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
        e = picked[rel_doc]
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


# ── Endpoint ────────────────────────────────────────────────────────────────


@router.post(
    "/collections/{collection_id}/search",
    response_model=SearchResponse,
)
async def search_collection(
    collection_id: int,
    payload: SearchRequest,
    request: Request,
    db: Session = Depends(get_db),
    principal: SearchPrincipal = Depends(resolve_search_principal),
    origin: CollectionOrigin = Depends(surface_origin_dep),
) -> SearchResponse:
    """Semantic top-K retrieval over one collection's chunks.

    One active clearance grant must cover level, compartments, collection
    membership and need-to-know. Admin/owner roles never bypass this data
    policy. Agent callers also need a sufficient ceiling and bound collection.
    """
    _enforce_agent_collection_scope(principal, collection_id)
    current_user = principal.user
    # End-user JWT/cookie path only — agent csk- and Studio runtime hops skip.
    # Denied on clearance 403; strict acceptance before embed; else success after retrieval.
    try:
        coll = _require_collection_clearance(
            db,
            principal=principal,
            collection_id=collection_id,
            origin=origin,
        )
    except HTTPException as exc:
        if (
            exc.status_code == 403
            and principal.agent is None
            and not principal.skip_inference_audit
        ):
            record_inference_audit(
                db,
                request=request,
                actor=current_user,
                action="inference.rag_query",
                resource_id=str(collection_id),
                detail=payload.query,
                status="denied",
                metadata={
                    "collection_id": collection_id,
                    "top_k": payload.top_k,
                    "min_score": payload.min_score,
                    "reason": short_audit_reason("clearance_denied"),
                },
                commit=True,
            )
        raise
    if coll.status != "active":
        if principal.agent is None and not principal.skip_inference_audit:
            record_inference_audit(
                db,
                request=request,
                actor=current_user,
                action="inference.rag_query",
                resource_id=str(collection_id),
                detail=payload.query,
                status="denied",
                metadata={
                    "collection_id": collection_id,
                    "top_k": payload.top_k,
                    "min_score": payload.min_score,
                    "reason": short_audit_reason("collection_inactive"),
                },
                commit=True,
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Collection {collection_id} is {coll.status}; reactivate before search.",
        )

    try:
        authorized_document_access = _authorized_document_access(
            db,
            principal=principal,
            collection_id=coll.id,
            requested_ids=payload.document_ids,
            reject_denied=payload.document_ids is not None,
        )
    except HTTPException as exc:
        if (
            exc.status_code == 403
            and principal.agent is None
            and not principal.skip_inference_audit
        ):
            record_inference_audit(
                db,
                request=request,
                actor=current_user,
                action="inference.rag_query",
                resource_id=str(collection_id),
                detail=payload.query,
                status="denied",
                metadata={
                    "collection_id": collection_id,
                    "top_k": payload.top_k,
                    "min_score": payload.min_score,
                    "reason": short_audit_reason("document_clearance_denied"),
                },
                commit=True,
            )
        raise
    rag_meta = {
        "collection_id": collection_id,
        "top_k": payload.top_k,
        "min_score": payload.min_score,
    }
    # Strict: write-ahead acceptance before embed/search side effects.
    # Non-strict: success only after retrieval completes (error on failure).
    acceptance_recorded = False
    end_user_audit = principal.agent is None and not principal.skip_inference_audit
    if end_user_audit:
        acceptance_recorded = record_at_acceptance(
            db,
            request=request,
            actor=current_user,
            action="inference.rag_query",
            resource_id=str(collection_id),
            detail=payload.query,
            metadata=rag_meta,
            commit=True,
            stream=False,
        )

    def _rag_outcome(status: str, *, reason: str | None = None) -> None:
        if not end_user_audit:
            return
        meta = dict(rag_meta)
        if reason:
            meta["reason"] = short_audit_reason(reason)
        record_at_outcome(
            db,
            request=request,
            actor=current_user,
            action="inference.rag_query",
            resource_id=str(collection_id),
            detail=payload.query,
            status=status,
            metadata=meta,
            commit=True,
            acceptance_recorded=acceptance_recorded,
        )

    if not authorized_document_access:
        _rag_outcome("success")
        return SearchResponse(
            query=payload.query,
            embedding_model=coll.embedding_model,
            embedding_fingerprint=coll.embedding_fingerprint,
            embedding_dim=coll.embedding_dim,
            results=[],
        )

    authorized_document_ceilings = {
        document_id: access.query_ceiling
        for document_id, access in authorized_document_access.items()
    }
    try:
        query_vec = await _embed_query(
            db,
            current_user,
            coll.embedding_model,
            coll.embedding_dim,
            payload.query,
            trusted_classification_level=Classification.max_of(
                access.required_classification
                for access in authorized_document_access.values()
            ),
        )

        try:
            pool = get_pool()
        except RuntimeError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc

        store = CollectionScopedPgVectorStore(pool, collection_id=coll.id)
        candidates = []
        if payload.document_ids is None:
            for ceiling, document_ids in _documents_by_ceiling(
                authorized_document_ceilings
            ).items():
                candidates.extend(
                    await store.similarity_search_scoped_documents(
                        query_embedding=query_vec,
                        document_ids=document_ids,
                        top_k=payload.top_k,
                        min_score=payload.min_score,
                        classification_ceiling=ceiling,
                    )
                )
        else:
            # G13: filter in SQL before ranking.  Ask for up to top_k per
            # document, then take the global top_k across that authorised set.
            # This prevents unrelated rows from consuming the HNSW top-k window.
            for ceiling, document_ids in _documents_by_ceiling(
                authorized_document_ceilings
            ).items():
                candidates.extend(
                    await store.similarity_search_per_document_authorized(
                        query_embedding=query_vec,
                        document_ids=document_ids,
                        classification_ceiling=ceiling,
                        k=payload.top_k,
                        min_score=payload.min_score,
                    )
                )
        hits = sorted(candidates, key=lambda hit: hit.score, reverse=True)[
            : payload.top_k
        ]

        if not hits:
            _rag_outcome("success")
            return SearchResponse(
                query=payload.query,
                embedding_model=coll.embedding_model,
                embedding_fingerprint=coll.embedding_fingerprint,
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
                principal=principal,
                collection_id=coll.id,
                main_doc_ids=doc_ids,
                query_vec=query_vec,
                payload=payload,
            )

        _rag_outcome("success")
        return SearchResponse(
            query=payload.query,
            embedding_model=coll.embedding_model,
            embedding_fingerprint=coll.embedding_fingerprint,
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
    except HTTPException as exc:
        if exc.status_code == 403:
            _rag_outcome("denied", reason="embedding_policy_denied")
        else:
            _rag_outcome(
                "error",
                reason=(
                    f"rag_http_{exc.status_code}"
                    if not isinstance(exc.detail, dict)
                    else str(
                        exc.detail.get("code") or f"rag_http_{exc.status_code}"
                    )
                ),
            )
        raise
    except Exception:
        _rag_outcome("error", reason="rag_retrieval_failed")
        raise


# ── Image search ────────────────────────────────────────────────────────────


@router.post(
    "/collections/{collection_id}/images/search",
    response_model=ImageSearchResponse,
)
async def search_collection_images(
    collection_id: int,
    payload: ImageSearchRequest,
    request: Request,
    db: Session = Depends(get_db),
    principal: SearchPrincipal = Depends(resolve_search_principal),
    origin: CollectionOrigin = Depends(surface_origin_dep),
) -> ImageSearchResponse:
    """Semantic top-K retrieval over a collection's ``ingestion_images`` rows.

    Phase 1 of the anila-studio extraction: this endpoint replaces the
    in-process ``app.api.studio._retrieve_images`` helper so the studio
    sub-service can pull image hits over HTTP instead of binding to the
    csp-db pgvector layer directly.

    Auth is identical to text search: clearance, compartments, collection
    grant, need-to-know and optional agent ceiling all apply before embedding.

    Returns ``results=[]`` (not an error) when:
      - the collection has zero indexed images (text-only KB),
      - the embedder returned an empty vector,
      - no rows beat the ``min_score`` threshold.
    """
    _enforce_agent_collection_scope(principal, collection_id)
    current_user = principal.user
    try:
        coll = _require_collection_clearance(
            db,
            principal=principal,
            collection_id=collection_id,
            origin=origin,
        )
    except HTTPException as exc:
        if (
            exc.status_code == 403
            and principal.agent is None
            and not principal.skip_inference_audit
        ):
            record_inference_audit(
                db,
                request=request,
                actor=current_user,
                action="inference.rag_query",
                resource_id=str(collection_id),
                detail=payload.query,
                status="denied",
                metadata={
                    "collection_id": collection_id,
                    "top_k": payload.top_k,
                    "min_score": payload.min_score,
                    "image_search": True,
                    "reason": short_audit_reason("clearance_denied"),
                },
                commit=True,
            )
        raise
    if coll.status != "active":
        if principal.agent is None and not principal.skip_inference_audit:
            record_inference_audit(
                db,
                request=request,
                actor=current_user,
                action="inference.rag_query",
                resource_id=str(collection_id),
                detail=payload.query,
                status="denied",
                metadata={
                    "collection_id": collection_id,
                    "top_k": payload.top_k,
                    "min_score": payload.min_score,
                    "image_search": True,
                    "reason": short_audit_reason("collection_inactive"),
                },
                commit=True,
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Collection {collection_id} is {coll.status}; reactivate before search.",
        )

    try:
        authorized_document_access = _authorized_document_access(
            db,
            principal=principal,
            collection_id=coll.id,
            requested_ids=payload.document_ids,
            reject_denied=payload.document_ids is not None,
        )
    except HTTPException as exc:
        if (
            exc.status_code == 403
            and principal.agent is None
            and not principal.skip_inference_audit
        ):
            record_inference_audit(
                db,
                request=request,
                actor=current_user,
                action="inference.rag_query",
                resource_id=str(collection_id),
                detail=payload.query,
                status="denied",
                metadata={
                    "collection_id": collection_id,
                    "top_k": payload.top_k,
                    "min_score": payload.min_score,
                    "image_search": True,
                    "reason": short_audit_reason("document_clearance_denied"),
                },
                commit=True,
            )
        raise
    rag_meta = {
        "collection_id": collection_id,
        "top_k": payload.top_k,
        "min_score": payload.min_score,
        "image_search": True,
    }
    acceptance_recorded = False
    end_user_audit = principal.agent is None and not principal.skip_inference_audit
    if end_user_audit:
        acceptance_recorded = record_at_acceptance(
            db,
            request=request,
            actor=current_user,
            action="inference.rag_query",
            resource_id=str(collection_id),
            detail=payload.query,
            metadata=rag_meta,
            commit=True,
            stream=False,
        )

    def _rag_outcome(status: str, *, reason: str | None = None) -> None:
        if not end_user_audit:
            return
        meta = dict(rag_meta)
        if reason:
            meta["reason"] = short_audit_reason(reason)
        record_at_outcome(
            db,
            request=request,
            actor=current_user,
            action="inference.rag_query",
            resource_id=str(collection_id),
            detail=payload.query,
            status=status,
            metadata=meta,
            commit=True,
            acceptance_recorded=acceptance_recorded,
        )

    if not authorized_document_access:
        _rag_outcome("success")
        return ImageSearchResponse(
            query=payload.query,
            embedding_model=coll.embedding_model,
            embedding_fingerprint=coll.embedding_fingerprint,
            embedding_dim=coll.embedding_dim,
            results=[],
        )

    try:
        q_vec = await _embed_query(
            db,
            current_user,
            coll.embedding_model,
            coll.embedding_dim,
            payload.query,
            trusted_classification_level=Classification.max_of(
                access.required_classification
                for access in authorized_document_access.values()
            ),
        )
        if not q_vec:
            _rag_outcome("success")
            return ImageSearchResponse(
                query=payload.query,
                embedding_model=coll.embedding_model,
                embedding_fingerprint=coll.embedding_fingerprint,
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
                  AND i.document_id = ANY($3::bigint[])
                  AND (i.embedding <=> $2) < $4
                ORDER BY i.embedding <=> $2
                LIMIT $5
                """,
                collection_id,
                q_value,
                list(authorized_document_access),
                max_dist,
                payload.top_k,
            )
    except HTTPException as exc:
        if exc.status_code == 403:
            _rag_outcome("denied", reason="embedding_policy_denied")
        else:
            _rag_outcome(
                "error",
                reason=(
                    f"rag_http_{exc.status_code}"
                    if not isinstance(exc.detail, dict)
                    else str(
                        exc.detail.get("code") or f"rag_http_{exc.status_code}"
                    )
                ),
            )
        raise
    except Exception:
        _rag_outcome("error", reason="rag_image_retrieval_failed")
        raise

    _rag_outcome("success")
    return ImageSearchResponse(
        query=payload.query,
        embedding_model=coll.embedding_model,
        embedding_fingerprint=coll.embedding_fingerprint,
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
