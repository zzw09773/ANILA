"""Ingestion collections CRUD (`/api/ingestion/collections`).

Sprint 4 refactor: collections are first-class user-owned resources.
Sprint 1–3 scoped them to ``agent_id``; that coupling was over-design
for the platform's "infra not multi-tenant SaaS" posture. CSP UI no
longer asks for an agent. Any agent backend points at a collection
via its own deploy config (``RAG_COLLECTION_ID`` env).

Authorisation:
- ``admin`` users: list / manage every collection (cross-org admin).
- non-admin: list / manage only collections they own (``created_by``).
  Sharing-with-other-users is a Sprint-5 ``collection_access_grants``
  concern; not in scope here.

Mutations write an ``audit_log`` row so misuse / accidental delete is
traceable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.ingestion import IngestionCollection
from app.models.user import User
from app.schemas.contracts.classification import ClassificationLevel
from app.schemas.ingestion import (
    CollectionCreate,
    CollectionResponse,
    CollectionUpdate,
)
from app.services.audit_service import log_audit_event
from app.services.auth_service import get_current_user, is_admin_tier

router = APIRouter(tags=["Ingestion / Collections"])
logger = logging.getLogger(__name__)

# Allowed product-surface tags (migration r1_0029 CHECK). Same vocabulary
# as conversations' ANILALM tag; CSP governance uses ``csp``.
_COLLECTION_ORIGINS = frozenset({"csp", "anilalm"})


# ── Authorisation helper ────────────────────────────────────────────────────


def _require_collection_access(
    db: Session, user: User, collection_id: int
) -> IngestionCollection:
    """Resolve the collection + confirm caller can manage it.

    Returns the row (callers usually need other fields anyway).
    Admin bypasses; non-admin must be the ``created_by`` owner. Future
    Sprint may add a ``collection_access_grants`` table for sharing
    across users; this helper is the single point that needs to grow
    when that lands.
    """
    coll = (
        db.query(IngestionCollection)
        .filter(IngestionCollection.id == collection_id)
        .first()
    )
    if coll is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Collection {collection_id} not found",
        )
    if is_admin_tier(user):
        return coll
    if coll.created_by != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"No access to collection {collection_id}",
        )
    return coll


# Back-compat alias so other endpoint files (documents.py / eval_runs.py /
# jobs.py) that still call ``_require_agent_access`` keep working until
# their Chunk Q sub-passes update them.
def _require_agent_access(db: Session, user: User, agent_id: int):  # noqa: ARG001
    """Sprint 4 deprecated — agent-scope access checks are gone.

    Existing callers pass ``coll.agent_id`` which was renamed away. To
    avoid breaking them mid-refactor, accept any int and grant access
    if the user is admin. Sub-passes in Chunk Q rewrite each caller to
    use ``_require_collection_access`` directly.
    """
    if is_admin_tier(user):
        return None
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="legacy _require_agent_access called; refactor to use _require_collection_access",
    )


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.post(
    "/api/ingestion/collections",
    response_model=CollectionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_collection(
    payload: CollectionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CollectionResponse:
    """Create a new (empty) collection owned by the calling user."""
    from app.services.platform_embedding import resolve_platform_embedding

    embedding_model = payload.embedding_model
    if not embedding_model:
        resolved = resolve_platform_embedding(db)
        if resolved is not None:
            embedding_model = resolved.name
        else:
            # Last-resort default so collection create never becomes a new
            # gate before an admin designates a platform embedding.
            embedding_model = "nvidia/NV-embed-V2"

    # Schema validator already normalised the label; re-parse so a future
    # schema drift cannot store a string the latch / bind rule reject.
    try:
        level = ClassificationLevel.from_storage(payload.classification_level)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="classification_level 必須是四級之一：無機密、營業秘密、密、機密",
        ) from exc

    origin = payload.origin or None
    if origin is not None and origin not in _COLLECTION_ORIGINS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="origin 必須是 'csp' 或 'anilalm'",
        )

    coll = IngestionCollection(
        name=payload.name,
        description=payload.description,
        chunking_config=payload.chunking_config.model_dump(),
        embedding_model=embedding_model,
        embedding_dim=payload.embedding_dim,
        status="active",
        document_count=0,
        chunk_count=0,
        bytes_stored=0,
        created_by=current_user.id,
        origin=origin,
        classification_level=level.to_storage(),
    )
    db.add(coll)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        # Log the raw driver error server-side; never leak schema / constraint
        # details (e.orig) to the API client.
        logger.warning("collection create IntegrityError: %s", e.orig)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Collection creation failed: a collection with these attributes may already exist.",
        ) from e
    db.refresh(coll)

    log_audit_event(
        db,
        commit=True,
        actor=current_user,
        action="ingestion_collection_create",
        resource_type="ingestion_collection",
        resource_id=coll.id,
        metadata={
            "name": payload.name,
            "created_by": current_user.id,
            "origin": origin,
            "classification_level": level.to_storage(),
        },
    )
    return CollectionResponse.model_validate(coll)


@router.get(
    "/api/ingestion/collections",
    response_model=list[CollectionResponse],
)
def list_collections(
    include_archived: bool = Query(
        False, description="預設只列 active；True 連 archived 一起回"
    ),
    owned_only: bool = Query(
        True,
        description=(
            "預設只列自己的 collections；admin 設 False 可看全部"
        ),
    ),
    origin: Optional[str] = Query(
        None,
        description=(
            "只列此產品面建立的知識庫（csp / anilalm）。"
            "NULL origin 的舊列仍會一併回傳，避免既有語料從貨架消失。"
        ),
    ),
    exclude_origin: Optional[str] = Query(
        None,
        description=(
            "排除此產品面。NULL origin 舊列保留。"
            "與 origin 互斥；語意比照 /api/conversations。"
        ),
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[CollectionResponse]:
    """List collections accessible to the current user.

    Sprint 4: no ``agent_id`` filter. Default behaviour:
    - non-admin: only own collections (admin-bypass when ``owned_only=False``
      is rejected for non-admins).
    - admin: own collections by default; pass ``owned_only=false`` to
      see every collection on the platform.

    Origin filter (r1_0029): same shape as conversations — CSP governance
    passes ``origin=csp``, ANILALM passes ``origin=anilalm``. Pre-origin
    rows (``origin IS NULL``) stay visible under every surface so an
    existing corpus is never orphaned.
    """
    if not owned_only and not is_admin_tier(current_user):
        raise HTTPException(
            status_code=403,
            detail="owned_only=false requires admin role",
        )
    if origin is not None and exclude_origin is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="origin and exclude_origin are mutually exclusive",
        )
    if origin is not None and origin not in _COLLECTION_ORIGINS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="origin 必須是 'csp' 或 'anilalm'",
        )
    if exclude_origin is not None and exclude_origin not in _COLLECTION_ORIGINS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="exclude_origin 必須是 'csp' 或 'anilalm'",
        )

    q = db.query(IngestionCollection)
    if owned_only:
        q = q.filter(IngestionCollection.created_by == current_user.id)
    if not include_archived:
        q = q.filter(IngestionCollection.status == "active")
    if origin is not None:
        q = q.filter(
            or_(
                IngestionCollection.origin == origin,
                IngestionCollection.origin.is_(None),
            )
        )
    elif exclude_origin is not None:
        q = q.filter(
            or_(
                IngestionCollection.origin.is_(None),
                IngestionCollection.origin != exclude_origin,
            )
        )
    rows = q.order_by(IngestionCollection.id).all()
    return [CollectionResponse.model_validate(r) for r in rows]


@router.get(
    "/api/ingestion/collections/{collection_id}",
    response_model=CollectionResponse,
)
def get_collection(
    collection_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CollectionResponse:
    coll = _require_collection_access(db, current_user, collection_id)
    return CollectionResponse.model_validate(coll)


@router.patch(
    "/api/ingestion/collections/{collection_id}",
    response_model=CollectionResponse,
)
def update_collection(
    collection_id: int,
    payload: CollectionUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CollectionResponse:
    coll = _require_collection_access(db, current_user, collection_id)

    changed: dict[str, object] = {}
    if payload.name is not None:
        coll.name = payload.name
        changed["name"] = payload.name
    if payload.description is not None:
        coll.description = payload.description
        changed["description"] = payload.description
    if payload.chunking_config is not None:
        coll.chunking_config = payload.chunking_config.model_dump()
        changed["chunking_config"] = coll.chunking_config
    if payload.status is not None:
        coll.status = payload.status
        changed["status"] = payload.status

    if not changed:
        return CollectionResponse.model_validate(coll)

    coll.updated_at = datetime.now(timezone.utc)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        logger.warning("collection update IntegrityError: %s", e.orig)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Collection update failed: the change conflicts with an existing collection.",
        ) from e
    db.refresh(coll)

    log_audit_event(
        db,
        commit=True,
        actor=current_user,
        action="ingestion_collection_update",
        resource_type="ingestion_collection",
        resource_id=coll.id,
        metadata={"changed": list(changed.keys())},
    )
    return CollectionResponse.model_validate(coll)


@router.delete(
    "/api/ingestion/collections/{collection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_collection(
    collection_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    """Hard delete a collection.

    CASCADE drops every document and chunk in pgvector. There is no
    soft-delete here — admin-or-owner only operation, and there's no
    audit benefit to keeping orphan rows because the audit_log has a
    timestamped record of the delete itself.
    """
    coll = _require_collection_access(db, current_user, collection_id)
    snapshot = {"name": coll.name, "created_by": coll.created_by}
    db.delete(coll)
    db.commit()
    log_audit_event(
        db,
        commit=True,
        actor=current_user,
        action="ingestion_collection_delete",
        resource_type="ingestion_collection",
        resource_id=collection_id,
        metadata=snapshot,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
