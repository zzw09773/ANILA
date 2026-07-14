"""Cross-document relations API (design v2 §8).

Endpoints (all under collection access — admin or owner; no reader role yet,
so list and mutations share ``_require_collection_access``):

  GET    /api/ingestion/collections/{id}/relations            list (+ resolved/ambiguous flags)
  POST   /api/ingestion/collections/{id}/relations            add a manual edge
  DELETE /api/ingestion/relations/{rel_id}?collection_id=…     delete a manual edge
  POST   /api/ingestion/collections/{id}/relations:reresolve   reconcile + re-extract

Mutations inherit global CSRF (``CsrfMiddleware``) for cookie callers; Bearer
(SDK) is exempt. Every mutation writes an audit_log row.

RLS: ``document_relations`` is FORCE-RLS (migration 0039) and CSP connects as
``csp_app`` in dev/prod, so each handler scopes the transaction with
``relation_resolver.scope_collection_rls`` before touching the table — without
it the policy silently returns zero rows. DELETE takes ``collection_id`` as a
query param precisely because we must scope RLS *before* we can look the edge
up by id (and we then assert the edge belongs to that collection).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.ingestion.collections import _require_collection_access
from app.database import get_db
from app.models.ingestion import DocumentRelation, IngestionDocument
from app.models.user import User
from app.schemas.ingestion import (
    DocumentRelationCreate,
    DocumentRelationResponse,
    ReresolveResponse,
)
from app.services import relation_resolver as rr
from app.services.audit_service import log_audit_event
from app.services.auth_service import get_current_user

router = APIRouter(tags=["Ingestion / Relations"])


def _titles_for(db: Session, collection_id: int) -> dict[int, str | None]:
    rows = (
        db.query(IngestionDocument.id, IngestionDocument.title)
        .filter(IngestionDocument.collection_id == collection_id)
        .all()
    )
    return {r.id: r.title for r in rows}


def _to_response(
    edge: DocumentRelation,
    titles: dict[int, str | None],
    ambiguous: bool,
) -> DocumentRelationResponse:
    return DocumentRelationResponse(
        id=edge.id,
        collection_id=edge.collection_id,
        src_document_id=edge.src_document_id,
        dst_document_id=edge.dst_document_id,
        dst_chunk_id=edge.dst_chunk_id,
        target_ref=edge.target_ref,
        relation_type=edge.relation_type,
        confidence=edge.confidence,
        source=edge.source,
        extractor_run_id=edge.extractor_run_id,
        evidence=edge.evidence,
        created_at=edge.created_at,
        created_by_user_id=edge.created_by_user_id,
        src_title=titles.get(edge.src_document_id),
        dst_title=titles.get(edge.dst_document_id) if edge.dst_document_id else None,
        resolved=edge.dst_document_id is not None,
        ambiguous=ambiguous,
    )


@router.get(
    "/api/ingestion/collections/{collection_id}/relations",
    response_model=list[DocumentRelationResponse],
)
def list_relations(
    collection_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[DocumentRelationResponse]:
    """List a collection's edges, newest first, with src/dst titles and the
    resolved / ambiguous flags the relations tab renders."""
    _require_collection_access(db, current_user, collection_id)
    rr.scope_collection_rls(db, collection_id)

    edges = (
        db.query(DocumentRelation)
        .filter(DocumentRelation.collection_id == collection_id)
        .order_by(DocumentRelation.id.desc())
        .all()
    )
    titles = _titles_for(db, collection_id)
    ambiguity = rr.annotate_ambiguity(db, collection_id=collection_id, edges=edges)
    return [_to_response(e, titles, ambiguity.get(e.id, False)) for e in edges]


@router.post(
    "/api/ingestion/collections/{collection_id}/relations",
    response_model=DocumentRelationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_relation(
    collection_id: int,
    payload: DocumentRelationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DocumentRelationResponse:
    """Add a manual edge (``source='manual'``). The source document must be in
    this collection; the target is a concrete ``dst_document_id`` or a free-text
    ``target_ref`` name (resolved now if it already matches, else back-filled)."""
    _require_collection_access(db, current_user, collection_id)
    rr.scope_collection_rls(db, collection_id)

    src = (
        db.query(IngestionDocument)
        .filter(
            IngestionDocument.id == payload.src_document_id,
            IngestionDocument.collection_id == collection_id,
        )
        .first()
    )
    if src is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="src_document_id is not a document in this collection",
        )

    try:
        edge = rr.create_manual_edge(
            db,
            collection_id=collection_id,
            src_document_id=payload.src_document_id,
            relation_type=payload.relation_type,
            dst_document_id=payload.dst_document_id,
            target_ref=payload.target_ref,
            evidence=payload.evidence,
            created_by_user_id=current_user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    log_audit_event(
        db,
        action="document_relation.create",
        resource_type="document_relation",
        actor=current_user,
        resource_id=edge.id,
        detail=f"{edge.src_document_id} -{edge.relation_type}-> {edge.target_ref}",
        metadata={"collection_id": collection_id, "source": "manual"},
    )
    db.commit()
    db.refresh(edge)

    titles = _titles_for(db, collection_id)
    ambiguity = rr.annotate_ambiguity(db, collection_id=collection_id, edges=[edge])
    return _to_response(edge, titles, ambiguity.get(edge.id, False))


@router.delete(
    "/api/ingestion/relations/{rel_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_relation(
    rel_id: int,
    collection_id: int = Query(
        ...,
        description=(
            "Collection the edge belongs to — required so we can scope RLS "
            "before the lookup. The edge is asserted to belong to it."
        ),
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a manual edge. Rule edges are managed by re-extract and cannot be
    deleted here (404). 404 too if the edge isn't in ``collection_id``."""
    _require_collection_access(db, current_user, collection_id)
    rr.scope_collection_rls(db, collection_id)

    edge = (
        db.query(DocumentRelation)
        .filter(
            DocumentRelation.id == rel_id,
            DocumentRelation.collection_id == collection_id,
        )
        .first()
    )
    if edge is None or edge.source != "manual":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No manual relation with that id in this collection",
        )

    rr.delete_manual_edge(db, rel_id=rel_id)
    log_audit_event(
        db,
        action="document_relation.delete",
        resource_type="document_relation",
        actor=current_user,
        resource_id=rel_id,
        metadata={"collection_id": collection_id},
    )
    db.commit()
    return None


@router.post(
    "/api/ingestion/collections/{collection_id}/relations:reresolve",
    response_model=ReresolveResponse,
)
async def reresolve_relations(
    collection_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ReresolveResponse:
    """Reconcile now + queue a full re-extract.

    The synchronous part re-matches every unresolved edge against current
    document titles (back-fills links that were waiting on a late upload) and
    returns its counts immediately. It also best-effort enqueues a worker job
    that re-parses each blob and re-extracts rule edges (the '重抽' half), whose
    results show up on the next list / search once the worker finishes."""
    _require_collection_access(db, current_user, collection_id)

    counts = rr.resolve_pending(db, collection_id=collection_id)

    job_id: str | None = None
    try:
        from app.services.ingestion_queue import enqueue_reresolve_relations

        job_id = await enqueue_reresolve_relations(collection_id, current_user.id)
    except Exception:  # noqa: BLE001 — re-extract is best-effort; reconcile already ran
        job_id = None

    log_audit_event(
        db,
        action="document_relation.reresolve",
        resource_type="ingestion_collection",
        actor=current_user,
        resource_id=collection_id,
        metadata={
            "resolved": counts.resolved,
            "ambiguous": counts.ambiguous,
            "reextract_job_id": job_id,
        },
    )
    db.commit()

    return ReresolveResponse(
        rule_edges_extracted=0,  # async; the worker reports its own count in logs
        resolved=counts.resolved,
        unresolved=counts.still_unresolved,
        ambiguous=counts.ambiguous,
    )
