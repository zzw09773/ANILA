"""Ingestion collections CRUD — dual-mounted under two product surfaces.

Relative paths (``/collections`` …) are mounted twice by ``app.api.router``:

* ``/api/ingestion/...`` → governance product, ``origin='csp'``
* ``/api/personal/...`` → ANILALM personal KB, ``origin='anilalm'``

Each surface lists / creates / resolves only its own origin. Creation
sets ``origin`` server-side from the mount; the client cannot choose it.

⚠ ``origin`` is product inventory partitioning, NOT an authorization
control. Ownership (``created_by`` / admin bypass) is unchanged. Same
user, same rights, different shelf. Do not "fix" the origin filter
out thinking it is over-isolation — the product rule is: a collection
created in CSP must not appear in ANILALM even when an individual
created it.

Authorisation (unchanged):
- ``admin`` users: list / manage every collection of this surface
  (cross-org admin within the surface).
- non-admin: list / manage only collections they own (``created_by``).
  Sharing-with-other-users is a Sprint-5 ``collection_access_grants``
  concern; not in scope here.

Mutations write an ``audit_log`` row so misuse / accidental delete is
traceable.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.config import settings
from app.models.ingestion import IngestionCollection, IngestionDocument, IngestionJob
from app.models.task import Task
from app.models.user import User
from app.schemas.ingestion import (
    CollectionCreate,
    CollectionResponse,
    CollectionUpdate,
)
from app.services.audit_service import log_audit_event
from app.services.auth_service import get_current_user, is_admin_tier
from app.modules.clearance.service import resolve_effective_classification_clearance
from app.modules.policy import apply_classification
from anila_contracts import Classification as ClassificationLevel
from app.schemas.contracts.policy import PolicyActorType
from app.api.ingestion.surface import (
    ANY_SURFACE,
    OriginArg,
    require_surface_origin,
)

router = APIRouter(tags=["Ingestion / Collections"])
logger = logging.getLogger(__name__)


# ── Surface-aware resolution ────────────────────────────────────────────────


def lookup_collection_for_surface(
    db: Session,
    collection_id: int,
    *,
    origin: OriginArg,
) -> IngestionCollection | None:
    """Fetch a collection row with an explicit surface decision.

    ``origin`` is required:

    * ``SURFACE_CSP`` / ``SURFACE_ANILALM`` — row with another origin is
      treated as missing (``None`` → callers raise 404).
    * ``ANY_SURFACE`` — deliberate cross-product lookup (named constant;
      greppable). Use only at sites that intentionally span products.

    ⚠ This origin filter is inventory partitioning, not authz.
    """
    q = db.query(IngestionCollection).filter(
        IngestionCollection.id == collection_id
    )
    if origin is not ANY_SURFACE:
        q = q.filter(IngestionCollection.origin == origin)
    return q.first()


def _require_collection_access(
    db: Session,
    user: User,
    collection_id: int,
    *,
    origin: OriginArg,
) -> IngestionCollection:
    """Resolve the collection + confirm caller can manage it.

    Returns the row (callers usually need other fields anyway).
    Admin bypasses; non-admin must be the ``created_by`` owner. Future
    Sprint may add a ``collection_access_grants`` table for sharing
    across users; this helper is the single point that needs to grow
    when that lands.

    ``origin`` is required (see ``lookup_collection_for_surface``). A
    row belonging to another product shelf is 404 (not 403). This is
    NOT a security boundary; see ``surface.py``.
    """
    coll = lookup_collection_for_surface(
        db, collection_id, origin=origin
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
    "/collections",
    response_model=CollectionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_collection(
    payload: CollectionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CollectionResponse:
    """Create a new (empty) collection owned by the calling user.

    ``origin`` is taken from the dual-mount surface — never from the
    request body.
    """
    surface_origin = require_surface_origin()
    fingerprint = settings.EMBEDDING_MODEL_FINGERPRINT.strip().lower()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Embedding weight fingerprint is not configured.",
        )
    if (
        payload.embedding_fingerprint is not None
        and payload.embedding_fingerprint != fingerprint
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Requested embedding fingerprint does not match deployment.",
        )
    # W2-11:建立知識庫的密等不得超過建立者 clearance(輸入端上限)。
    # 無有效 clearance grant 時只能建「無機密」(fail-closed)。
    declared = payload.classification_level or ClassificationLevel.UNCLASSIFIED
    ceiling = resolve_effective_classification_clearance(
        db, user_id=current_user.id
    )
    max_allowed = ceiling or ClassificationLevel.UNCLASSIFIED
    if declared.rank > max_allowed.rank:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"知識庫密等「{declared.to_storage()}」超過建立者 clearance"
                f"（上限「{max_allowed.to_storage()}」）"
            ),
        )
    coll = IngestionCollection(
        name=payload.name,
        description=payload.description,
        chunking_config=payload.chunking_config.model_dump(),
        embedding_model=payload.embedding_model,
        embedding_fingerprint=fingerprint,
        embedding_dim=payload.embedding_dim,
        status="active",
        document_count=0,
        chunk_count=0,
        bytes_stored=0,
        created_by=current_user.id,
        origin=surface_origin,
        classification_level=declared.to_storage(),
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
            "origin": surface_origin,
            "classification_level": declared.to_storage(),
        },
    )
    return CollectionResponse.model_validate(coll)


@router.get(
    "/collections",
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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[CollectionResponse]:
    """List collections accessible to the current user on this surface.

    Always filtered to this mount's ``origin``. Ownership filter
    (``created_by`` / admin ``owned_only=false``) is unchanged.
    """
    surface_origin = require_surface_origin()
    if not owned_only and not is_admin_tier(current_user):
        raise HTTPException(
            status_code=403,
            detail="owned_only=false requires admin role",
        )

    q = db.query(IngestionCollection).filter(
        IngestionCollection.origin == surface_origin
    )
    if owned_only:
        q = q.filter(IngestionCollection.created_by == current_user.id)
    if not include_archived:
        q = q.filter(IngestionCollection.status == "active")
    rows = q.order_by(IngestionCollection.id).all()
    return [CollectionResponse.model_validate(r) for r in rows]


@router.get(
    "/collections/{collection_id}",
    response_model=CollectionResponse,
)
def get_collection(
    collection_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CollectionResponse:
    coll = _require_collection_access(
        db, current_user, collection_id, origin=require_surface_origin()
    )
    return CollectionResponse.model_validate(coll)


@router.patch(
    "/collections/{collection_id}",
    response_model=CollectionResponse,
)
def update_collection(
    collection_id: int,
    payload: CollectionUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CollectionResponse:
    coll = _require_collection_access(
        db, current_user, collection_id, origin=require_surface_origin()
    )

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
    if payload.classification_level is not None:
        if not is_admin_tier(current_user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="僅 admin/owner 可變更知識庫分類等級",
            )
        previous = ClassificationLevel.from_storage(coll.classification_level)
        target = payload.classification_level
        if target != previous:
            if target < previous:
                # 降級唯一合法路徑:降密申請 → 主管核准(雙人原則)。
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "知識庫分類不可直接降級；請透過降密申請流程 "
                        "POST /api/classification/declassification-requests "
                        "經主管核准後生效（雙人原則）。"
                    ),
                )
            # 升級走平台單向閂鎖核心(FOR UPDATE + ClassificationEvent +
            # latch 欄位);不得另記 collection.read PolicyDecision。
            apply_classification(
                db,
                resource_type="collection",
                resource_id=str(coll.id),
                new_level=target.to_storage(),
                actor_type=PolicyActorType.USER.value,
                actor_id=str(current_user.id),
                reason="manual_admin",
                source="manual_admin",
                commit=False,
            )
            changed["classification_level"] = target.to_storage()

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
    "/collections/{collection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_collection(
    collection_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    """Archive a collection; the leased retention reaper performs erasure."""
    coll = _require_collection_access(
        db, current_user, collection_id, origin=require_surface_origin()
    )
    active_job = db.query(IngestionJob.id).filter(
        IngestionJob.collection_id == coll.id,
        IngestionJob.status.notin_(("succeeded", "failed", "cancelled", "dead_letter")),
    ).first()
    active_tasks = db.query(Task).filter(
        Task.status.notin_(("completed", "failed", "cancelled", "blocked_by_policy"))
    ).all()
    if active_job is not None or any(
        coll.id in (task.selected_collection_ids or []) for task in active_tasks
    ):
        raise HTTPException(status_code=409, detail="Collection has active work")
    snapshot = {"name": coll.name, "created_by": coll.created_by, "origin": coll.origin}
    if coll.lifecycle_state == "erased":
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    now = datetime.now(timezone.utc)
    erase_due = now + timedelta(days=settings.RETENTION_INGESTION_ARCHIVE_DAYS)
    coll.status = "archived"
    coll.lifecycle_state = "archived"
    coll.archived_at = coll.archived_at or now
    coll.erase_due_at = erase_due
    db.query(IngestionDocument).filter(
        IngestionDocument.collection_id == coll.id,
        IngestionDocument.lifecycle_state == "active",
    ).update({
        IngestionDocument.lifecycle_state: "archived",
        IngestionDocument.archived_at: now,
        IngestionDocument.erase_due_at: erase_due,
        IngestionDocument.availability_status: "unavailable",
    }, synchronize_session=False)
    db.commit()
    log_audit_event(
        db,
        commit=True,
        actor=current_user,
        action="ingestion_collection_archive",
        resource_type="ingestion_collection",
        resource_id=collection_id,
        metadata=snapshot,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
