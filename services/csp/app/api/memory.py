"""Per-user memory inspection & control endpoints.

GDPR / 個資法 compliance surface for the memory system added in
P1 (``app.services.memory_service``). Every authenticated user can:

* See exactly what facts the platform has remembered about them.
* See a recent slice of which conversation messages got embedded
  for cross-conversation recall.
* Delete a single fact or wipe their entire memory.

All routes are scoped to ``current_user.id`` — there's no admin
override path here. An admin who needs to inspect another user's
memory should query the DB directly with an audit-logged session;
exposing it via REST would be a privacy regression.

The chunk endpoints intentionally don't expose the embedding vector
itself (4000 floats per row × hundreds of rows = a huge response
that's also useless to a human). Operators who need vectors hit
the DB directly.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.models.user_memory import ConversationMemoryChunk, UserFact
from app.services.auth_service import get_current_user
from app.services.memory_service import (
    MemoryPolicyDataError,
    get_authorized_chunk_rows,
    get_user_facts,
)
from anila_contracts import Classification

router = APIRouter(prefix="/api/memory", tags=["memory"])


# Default cap on the chunk preview list. The UI table doesn't paginate
# in MVP, so we don't want a single response carrying thousands of
# multi-KB strings. Operators can override per-request via ``?limit=``
# up to ``_CHUNK_LIMIT_MAX``; beyond that point the recommended
# workflow is "wipe and start fresh" rather than scroll.
_CHUNK_LIMIT_DEFAULT = 50
_CHUNK_LIMIT_MAX = 500

# Trim each preview to keep the total payload reasonable. A real user
# turn often runs 1-3 KB; an unbounded list of those drives the
# settings dialog into a janky scroll.
_PREVIEW_CHARS = 240


# ── Schemas ───────────────────────────────────────────────────────────────────


class FactResponse(BaseModel):
    id: int
    key: str
    value: str
    confidence: float
    source_conversation_id: Optional[int] = None
    source_message_id: Optional[int] = None
    classification_level: str
    classification_source: str
    source_task_id: Optional[int] = None
    source_snapshot_id: Optional[int] = None
    required_compartment_ids: list[int]
    source_collection_ids: list[int]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class FactListResponse(BaseModel):
    total: int
    facts: list[FactResponse]


class ChunkPreviewResponse(BaseModel):
    id: int
    conversation_id: int
    role: str
    content: str  # already trimmed to _PREVIEW_CHARS
    is_encrypted: bool
    classification_level: str
    classification_source: str
    source_task_id: Optional[int] = None
    source_snapshot_id: Optional[int] = None
    required_compartment_ids: list[int]
    source_collection_ids: list[int]
    created_at: datetime


class ChunkListResponse(BaseModel):
    total: int
    encrypted_total: int
    distinct_conversations: int
    items: list[ChunkPreviewResponse]


class DeleteResponse(BaseModel):
    deleted: int


# ── Facts ─────────────────────────────────────────────────────────────────────


@router.get("/facts", response_model=FactListResponse)
def list_facts(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """All structured facts remembered for the current user.

    No pagination — fact count is bounded in practice (one row per
    extracted key; LLMs converge on a small key vocabulary). If a
    deployment ever sees thousands of facts per user, the right fix
    is consolidation, not pagination.
    """
    try:
        rows = get_user_facts(db, current_user.id)
    except MemoryPolicyDataError as exc:
        raise HTTPException(
            status_code=503,
            detail="Memory 治理資料不完整，已依 fail-closed 拒絕讀取",
        ) from exc
    return FactListResponse(
        total=len(rows),
        facts=[
            FactResponse(
                id=int(row.id),
                key=row.key,
                value=row.value,
                confidence=row.confidence,
                source_conversation_id=row.source_conversation_id,
                source_message_id=row.source_message_id,
                classification_level=row.classification_level.to_storage(),
                classification_source=row.classification_source,
                source_task_id=row.source_task_id,
                source_snapshot_id=row.source_snapshot_id,
                required_compartment_ids=sorted(row.required_compartment_ids),
                source_collection_ids=sorted(row.source_collection_ids),
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in rows
        ],
    )


@router.delete("/facts/{fact_id}", response_model=DeleteResponse)
def delete_fact(
    fact_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a single fact owned by the current user.

    404 (not 403) on cross-user IDs so we don't reveal which IDs
    exist for other users.
    """
    fact = (
        db.query(UserFact)
        .filter(UserFact.id == fact_id, UserFact.user_id == current_user.id)
        .first()
    )
    if not fact:
        raise HTTPException(status_code=404, detail="Fact 不存在")
    db.delete(fact)
    db.commit()
    return DeleteResponse(deleted=1)


@router.delete("/facts", response_model=DeleteResponse)
def clear_facts(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Wipe every fact for the current user. Irreversible."""
    deleted = (
        db.query(UserFact)
        .filter(UserFact.user_id == current_user.id)
        .delete(synchronize_session=False)
    )
    db.commit()
    return DeleteResponse(deleted=int(deleted))


# ── Chunks (cross-conversation RAG) ───────────────────────────────────────────


@router.get("/chunks", response_model=ChunkListResponse)
def list_chunks(
    limit: int = Query(_CHUNK_LIMIT_DEFAULT, ge=1, le=_CHUNK_LIMIT_MAX),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Recent chunk preview + headline counts.

    Returns the ``limit`` newest chunks (each ``content`` trimmed to
    ``_PREVIEW_CHARS``) plus three roll-ups:

    * ``total`` — every chunk for this user (used by the UI to
      surface a "we have N memories of you" headline).
    * ``encrypted_total`` — drives the privacy banner about whether
      any classified material is in the pool.
    * ``distinct_conversations`` — gives a sense of how broad the
      recall is, helps the user decide whether wiping is overkill.
    """
    try:
        authorized = get_authorized_chunk_rows(db, current_user.id)
    except MemoryPolicyDataError as exc:
        raise HTTPException(
            status_code=503,
            detail="Memory 治理資料不完整，已依 fail-closed 拒絕讀取",
        ) from exc
    total = len(authorized)
    encrypted_total = sum(
        level >= Classification.CONFIDENTIAL
        for _row, level, _compartments, _collections in authorized
    )
    distinct_conv_rows = len({int(row.conversation_id) for row, *_ in authorized})
    rows = authorized[:limit]

    items: list[ChunkPreviewResponse] = []
    for r, level, compartments, collections in rows:
        content = r.content or ""
        if len(content) > _PREVIEW_CHARS:
            content = content[:_PREVIEW_CHARS] + "…"
        items.append(
            ChunkPreviewResponse(
                id=int(r.id),
                conversation_id=int(r.conversation_id),
                role=str(r.role),
                content=content,
                is_encrypted=bool(
                    r.is_encrypted or level >= Classification.CONFIDENTIAL
                ),
                classification_level=level.to_storage(),
                classification_source=str(r.classification_source),
                source_task_id=r.source_task_id,
                source_snapshot_id=r.source_snapshot_id,
                required_compartment_ids=sorted(compartments),
                source_collection_ids=sorted(collections),
                created_at=r.created_at,
            )
        )
    return ChunkListResponse(
        total=total,
        encrypted_total=encrypted_total,
        distinct_conversations=distinct_conv_rows,
        items=items,
    )


@router.delete("/chunks", response_model=DeleteResponse)
def clear_chunks(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Wipe every memory chunk for the current user. Irreversible.

    Facts are NOT touched — those live in their own table and have
    a separate clear endpoint. We keep the two operations distinct
    because users often want to forget recent discussions
    (chunks) while keeping the high-signal extracted facts
    (preferences, name, role).
    """
    deleted = (
        db.query(ConversationMemoryChunk)
        .filter(ConversationMemoryChunk.user_id == current_user.id)
        .delete(synchronize_session=False)
    )
    db.commit()
    return DeleteResponse(deleted=int(deleted))


