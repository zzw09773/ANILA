"""Ingestion collections CRUD (`/api/ingestion/collections`).

Sprint 1 (sync API, no jobs): create / list / detail / update / delete
operations on ``IngestionCollection`` rows. Document upload and chunk
indexing live in Sprint 2 once the worker queue is up — POSTing a
collection in Sprint 1 just creates the metadata row, not any chunks.

Authorisation model:

- ``admin`` users can manage any agent's collections.
- Non-admin users need ``UserAgentPermission`` on the collection's
  ``agent_id``. Same gate the chat path uses for the agent itself,
  so "if you can talk to the agent you can manage its corpus" is the
  default — adjust per-collection ACL is a Sprint 2+ concern.

Mutations also write an ``audit_log`` row so a misuse / accidental
delete is traceable to a user.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.agent import Agent, UserAgentPermission
from app.models.ingestion import IngestionCollection
from app.models.user import User
from app.schemas.ingestion import (
    CollectionCreate,
    CollectionResponse,
    CollectionUpdate,
)
from app.services.audit_service import log_audit_event
from app.services.auth_service import get_current_user

router = APIRouter(tags=["Ingestion / Collections"])


# ── Authorisation helper ────────────────────────────────────────────────────


def _require_agent_access(db: Session, user: User, agent_id: int) -> Agent:
    """Resolve the agent and confirm caller has access.

    Centralised so every endpoint applies the same rule. Returns the
    Agent row (fetched anyway for FK validation) so callers can read
    e.g. agent.name for audit messages.

    Admin bypasses; non-admin requires ``UserAgentPermission``.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {agent_id} not found",
        )

    if user.role == "admin":
        return agent

    perm = (
        db.query(UserAgentPermission)
        .filter(
            UserAgentPermission.user_id == user.id,
            UserAgentPermission.agent_id == agent_id,
        )
        .first()
    )
    if perm is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"No access to agent {agent_id}",
        )
    return agent


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
    """Create a new (empty) collection scoped to ``payload.agent_id``."""
    _require_agent_access(db, current_user, payload.agent_id)

    coll = IngestionCollection(
        agent_id=payload.agent_id,
        name=payload.name,
        description=payload.description,
        chunking_config=payload.chunking_config.model_dump(),
        embedding_model=payload.embedding_model,
        embedding_dim=payload.embedding_dim,
        status="active",
        document_count=0,
        chunk_count=0,
        bytes_stored=0,
        created_by=current_user.id,
    )
    db.add(coll)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        # The only realistic IntegrityError here is the
        # ``uq_collections_agent_name`` collision — translate to 409.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Collection name '{payload.name}' already exists for agent "
                f"{payload.agent_id}"
            ),
        ) from e
    db.refresh(coll)

    log_audit_event(
        db,
        actor=current_user,
        action="ingestion_collection_create",
        resource_type="ingestion_collection",
        resource_id=coll.id,
        metadata={"agent_id": payload.agent_id, "name": payload.name},
    )
    return CollectionResponse.model_validate(coll)


@router.get(
    "/api/ingestion/collections",
    response_model=list[CollectionResponse],
)
def list_collections(
    agent_id: int = Query(..., description="必填：列出此 agent 的 collections"),
    include_archived: bool = Query(
        False, description="預設只列 active；True 連 archived 一起回"
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[CollectionResponse]:
    """List collections for one agent.

    ``agent_id`` is required (not optional) because the API never has a
    legitimate "list every agent's collections" use case — that would
    cross tenant boundaries. Admin who wants to scan all agents calls
    this endpoint per agent.
    """
    _require_agent_access(db, current_user, agent_id)

    q = db.query(IngestionCollection).filter(
        IngestionCollection.agent_id == agent_id
    )
    if not include_archived:
        q = q.filter(IngestionCollection.status == "active")

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
    coll = db.query(IngestionCollection).filter(
        IngestionCollection.id == collection_id
    ).first()
    if coll is None:
        raise HTTPException(status_code=404, detail="Collection not found")
    _require_agent_access(db, current_user, coll.agent_id)
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
    coll = db.query(IngestionCollection).filter(
        IngestionCollection.id == collection_id
    ).first()
    if coll is None:
        raise HTTPException(status_code=404, detail="Collection not found")
    _require_agent_access(db, current_user, coll.agent_id)

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
        # Same uniqueness translation as create — name collision.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Name collision with another collection on the same agent",
        ) from e
    db.refresh(coll)

    log_audit_event(
        db,
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
    soft-delete here — admin-only operation per UI gating, and there's
    no audit benefit to keeping orphan rows because the audit_log has
    a timestamped record of the delete itself.
    """
    coll = db.query(IngestionCollection).filter(
        IngestionCollection.id == collection_id
    ).first()
    if coll is None:
        raise HTTPException(status_code=404, detail="Collection not found")
    _require_agent_access(db, current_user, coll.agent_id)

    snapshot = {"agent_id": coll.agent_id, "name": coll.name}
    db.delete(coll)
    db.commit()

    log_audit_event(
        db,
        actor=current_user,
        action="ingestion_collection_delete",
        resource_type="ingestion_collection",
        resource_id=collection_id,
        metadata=snapshot,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
