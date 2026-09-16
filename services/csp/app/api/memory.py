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

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.models.user_memory import ConversationMemoryChunk, UserFact
from app.services.auth_service import get_current_user
from app.schemas.base import ApiResponseModel
from app.services.memory_service import REPLY_STYLE_KEY, REPLY_STYLE_MAX_CHARS

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


class FactResponse(ApiResponseModel):
    id: int
    key: str
    value: str
    confidence: float
    source_conversation_id: Optional[int] = None
    source_message_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class FactListResponse(BaseModel):
    total: int
    facts: list[FactResponse]


class ChunkPreviewResponse(ApiResponseModel):
    id: int
    conversation_id: int
    role: str
    content: str  # already trimmed to _PREVIEW_CHARS
    is_encrypted: bool
    created_at: datetime


class ChunkListResponse(BaseModel):
    total: int
    encrypted_total: int
    distinct_conversations: int
    items: list[ChunkPreviewResponse]


class DeleteResponse(BaseModel):
    deleted: int


class PreferenceBody(BaseModel):
    text: str = ""


class PreferenceResponse(BaseModel):
    text: str
    updated_at: Optional[datetime] = None


def _preference_row(db: Session, user_id: int) -> Optional[UserFact]:
    return (
        db.query(UserFact)
        .filter(UserFact.user_id == user_id, UserFact.key == REPLY_STYLE_KEY)
        .first()
    )


@router.get("/preference", response_model=PreferenceResponse)
def get_preference(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """User-authored reply style. Empty string when the user has not set one."""
    row = _preference_row(db, current_user.id)
    if not row:
        return PreferenceResponse(text="")
    return PreferenceResponse(text=row.value, updated_at=row.updated_at)


@router.put("/preference", response_model=PreferenceResponse)
def put_preference(
    body: PreferenceBody,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Write or clear the reply-style fact. Blank text deletes the row."""
    text = (body.text or "").strip()
    if len(text) > REPLY_STYLE_MAX_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"回覆偏好最長 {REPLY_STYLE_MAX_CHARS} 字",
        )
    if not text:
        row = _preference_row(db, current_user.id)
        if row:
            db.delete(row)
            db.commit()
        return PreferenceResponse(text="")
    row = _preference_row(db, current_user.id)
    if row:
        row.value = text
        row.confidence = 1.0
        row.source_conversation_id = None
        row.source_message_id = None
        row.updated_at = datetime.now(timezone.utc)
    else:
        fields = {
            "user_id": current_user.id,
            "key": REPLY_STYLE_KEY,
            "value": text,
            "confidence": 1.0,
        }
        # SQLite 測試庫的 BigInteger PK 不會自動加號；Postgres 走 identity。
        if db.get_bind().dialect.name == "sqlite":
            fields["id"] = int(db.query(func.max(UserFact.id)).scalar() or 0) + 1
        row = UserFact(**fields)
        db.add(row)
    db.commit()
    db.refresh(row)
    return PreferenceResponse(text=row.value, updated_at=row.updated_at)


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
    rows = (
        db.query(UserFact)
        .filter(UserFact.user_id == current_user.id)
        .order_by(UserFact.updated_at.desc())
        .all()
    )
    return FactListResponse(
        total=len(rows),
        facts=[FactResponse.model_validate(r) for r in rows],
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
    """Wipe extracted facts. The user-authored reply style is kept."""
    deleted = (
        db.query(UserFact)
        .filter(
            UserFact.user_id == current_user.id,
            UserFact.key != REPLY_STYLE_KEY,
        )
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
    base_q = db.query(ConversationMemoryChunk).filter(
        ConversationMemoryChunk.user_id == current_user.id
    )
    total = base_q.count()
    encrypted_total = base_q.filter(
        ConversationMemoryChunk.is_encrypted.is_(True)
    ).count()
    distinct_conv_rows = (
        base_q.with_entities(ConversationMemoryChunk.conversation_id)
        .distinct()
        .count()
    )
    rows = base_q.order_by(ConversationMemoryChunk.id.desc()).limit(limit).all()

    items: list[ChunkPreviewResponse] = []
    for r in rows:
        content = r.content or ""
        if len(content) > _PREVIEW_CHARS:
            content = content[:_PREVIEW_CHARS] + "…"
        items.append(
            ChunkPreviewResponse(
                id=int(r.id),
                conversation_id=int(r.conversation_id),
                role=str(r.role),
                content=content,
                is_encrypted=bool(r.is_encrypted),
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


