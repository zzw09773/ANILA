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

from app.api.agents._common import effective_agent_policy_level
from app.database import get_db
from app.models.ingestion import IngestionCollection
from app.models.user import User
from app.modules.policy import apply_classification
from app.schemas.contracts.classification import ClassificationLevel
from app.schemas.ingestion import (
    CollectionClassificationRaise,
    CollectionCreate,
    CollectionResponse,
    CollectionUpdate,
)
from app.services.audit_service import log_audit_event
from app.services.auth_service import get_current_user, is_admin_tier
from app.services.ingestion_classification import (
    agents_bound_below_level,
    cascade_raise_documents,
    unreadable_classification_rows,
)

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


@router.post(
    "/api/ingestion/collections/{collection_id}/classification",
    response_model=CollectionResponse,
)
def raise_collection_classification(
    collection_id: int,
    payload: CollectionClassificationRaise,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CollectionResponse:
    """Raise a collection's classification level (one-way latch).

    Goes through ``apply_classification`` — never writes the column
    directly — so ClassificationEvent + memory-purge side effects stay
    on the single latch path. Lowering is refused with a pointer to the
    declassification flow (``apply_classification`` would no-op; we turn
    that into an explicit error instead of a misleading 200).

    Before writing: refuse if any bound agent is below the new level
    (name the agents; do not silently raise them). Documents below the
    new level are cascaded via ``apply_classification`` in the **same
    transaction** as the collection's own latch — all-or-nothing.

    Atomicity (this is the whole point of the ``commit=False`` plumbing):
    the latch is one-way, so a half-applied raise is not a retryable
    blip — it strands documents at a level only the three-party
    declassification flow can undo, one document at a time, while the
    caller sees a failure and retries into what looks like success. So
    every write here lives in one transaction and there is exactly one
    ``db.commit()``. Any failure rolls the whole set back.

    Atomicity does **not** depend on row locking: ``apply_classification``
    does take ``SELECT … FOR UPDATE`` (a no-op under SQLite), but what
    makes this all-or-nothing is the single enclosing transaction.
    Locking only narrows the concurrent-raise window, and that case is
    handled explicitly by the ``event is None`` branch below.

    Known cost of that choice: the transaction (and, under Postgres, the
    row locks on every cascaded document) lives for the whole cascade,
    so a raise on a very large collection is one long write transaction.
    That is the price of not stranding documents, and this is a rare,
    admin-triggered, non-streaming operation. If collections ever get
    large enough for it to matter, the answer is batching with a
    resumable record of what advanced — not going back to per-document
    commits, which is the bug this replaced.

    Auth matches other collection mutations (owner or admin-tier). Not
    looser than ``create_collection`` (any authenticated user may create
    at any level today).
    """
    coll = _require_collection_access(db, current_user, collection_id)
    # The Pydantic validator on ``CollectionClassificationRaise`` already
    # rejects the four-value violation with 422 before the route body runs,
    # so no defensive re-parse of ``payload`` is needed here.
    target = ClassificationLevel.from_storage(payload.classification_level)

    stored_previous = getattr(coll, "classification_level", None) or "無機密"
    try:
        previous = ClassificationLevel.from_storage(stored_previous)
    except ValueError as exc:
        # Only reachable if something wrote the column outside the app.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"知識庫 #{collection_id} 的密等儲存值「{stored_previous}」不是"
                f"四級之一（無機密／營業秘密／密／機密），無法判斷是否為升密，"
                f"因此整批拒絕、未做任何變更。請先修正該筆資料再重試。"
            ),
        ) from exc

    if target < previous:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"知識庫密等只能往上調（目前「{previous.to_storage()}」，"
                f"請求「{target.to_storage()}」）。"
                f"降級請走降密申請流程"
                f"（POST /api/classification/declassification-requests），"
                f"須主管核准，不可由此路由自行降級。"
            ),
        )
    if target == previous:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"知識庫已是「{previous.to_storage()}」，無須升密。"
                f"若要降級，請走降密申請流程。"
            ),
        )

    under = agents_bound_below_level(db, collection_id, target)
    if under:
        named = "、".join(
            f"「{a.name}」(id={a.id}，有效密等「"
            f"{effective_agent_policy_level(a).to_storage()}」)"
            for a in under
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"無法將知識庫升至「{target.to_storage()}」：下列已綁定的 "
                f"agent 有效密等低於目標等級：{named}。"
                f"請先透過 POST /api/agents/{{id}}/classification "
                f"將那些 agent 升至「{target.to_storage()}」以上，"
                f"再重試本知識庫升密。系統不會自動提升 agent。"
            ),
        )

    # Pre-flight: a document row whose stored level is not one of the four
    # values would blow up mid-cascade. Refuse the whole raise and name the
    # rows rather than half-applying — "silently skipping" would leave a
    # document below its collection, which is invariant (a) inverted.
    unreadable = unreadable_classification_rows(db, collection_id)
    if unreadable:
        named = "、".join(
            f"文件 #{doc_id}（儲存值「{stored}」）" for doc_id, stored in unreadable
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"無法升密：下列文件的密等儲存值不是四級之一"
                f"（無機密／營業秘密／密／機密）：{named}。"
                f"整批拒絕、未做任何變更——修正這些資料列後再重試。"
            ),
        )

    # ── single transaction: documents + collection + audit ──────────────
    try:
        raised_doc_ids = cascade_raise_documents(
            db,
            collection_id=collection_id,
            new_level=target,
            actor_user_id=current_user.id,
            commit=False,
        )

        event = apply_classification(
            db,
            resource_type="collection",
            resource_id=str(collection_id),
            new_level=target.to_storage(),
            actor_type="user",
            actor_id=str(current_user.id),
            reason="manual_admin",
            source="manual_admin",
            commit=False,
        )
        if event is None:
            # Concurrent raise or stale session. Roll the cascade back too —
            # otherwise the caller gets 409 while documents stayed raised.
            db.rollback()
            db.refresh(coll)
            current = ClassificationLevel.from_storage(
                getattr(coll, "classification_level", None) or "無機密"
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"知識庫密等未變更（目前「{current.to_storage()}」；"
                    f"請求「{target.to_storage()}」）。若目標未高於現行等級，"
                    f"升密不會生效；降級請走降密申請流程。"
                    f"本次未變更任何文件密等。"
                ),
            )

        log_audit_event(
            db,
            commit=False,
            actor=current_user,
            action="ingestion_collection_classification_raise",
            resource_type="ingestion_collection",
            resource_id=coll.id,
            metadata={
                "name": coll.name,
                "from_level": previous.to_storage(),
                "to_level": target.to_storage(),
                "classification_event_id": event.id,
                "cascaded_document_ids": raised_doc_ids,
            },
        )
        db.commit()
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        logger.exception(
            "collection classification raise failed collection_id=%s %s→%s",
            collection_id,
            previous.to_storage(),
            target.to_storage(),
        )
        # 說清楚「什麼都沒動」是重點——沒有訊息的 500 會讓操作者直覺重試，
        # 而重試在半套狀態下會成功並看起來正常。錯誤內文只給例外類型，
        # 細節留在伺服器日誌（不把內部訊息回給呼叫端）。
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"升密失敗，已整批回復：知識庫仍為「{previous.to_storage()}」，"
                f"庫內文件密等一律未變更，重試是安全的。"
                f"錯誤類型 {type(exc).__name__}，細節見伺服器日誌。"
            ),
        ) from exc

    db.refresh(coll)
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
