"""Conversation management endpoints (JWT auth)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.database import get_db
from app.models.attachment import Attachment
from app.models.conversation import Conversation, ConversationShare
from app.models.message import Message
from app.models.user import User
from app.services import conversation_service as svc

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class ConversationCreate(BaseModel):
    title: str = Field("新對話", max_length=255)
    agent_id: Optional[int] = None
    # Free-form tag for the calling frontend. None / '' is treated as
    # "unspecified". ANILALM sends 'anilalm', ANILA UI sends 'anila-ui'.
    # Future apps can pick any short identifier; see migration 0023.
    origin: Optional[str] = Field(default=None, max_length=32)
    # Knowledge-base scope. REQUIRED when origin='anilalm' (the LM
    # sidebar filters on it); MUST be None for any other origin
    # (anila-ui has no collection concept). The endpoint enforces this
    # contract; clients passing the wrong combination get 400.
    collection_id: Optional[int] = Field(default=None, ge=1)


class ConversationUpdate(BaseModel):
    title: str = Field(..., max_length=255)


class AttachmentOut(BaseModel):
    reference_id: str
    filename: str
    content_type: str
    size_bytes: int
    model_config = {"from_attributes": True}


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    trace_id: Optional[str]
    latency_ms: Optional[int]
    model_name: Optional[str]
    agent_name: Optional[str]
    # SQLAlchemy declarative classes expose a reserved ``metadata`` attribute
    # (the Table's MetaData), so the ORM column is stored under ``metadata_``.
    # ``validation_alias`` is the pydantic v2 knob that applies the alias
    # ONLY on the input side (from-attributes lookup); the output key stays
    # as the field name ``metadata``. The prior ``alias="metadata_"`` made
    # FastAPI emit ``metadata_`` AND its ``jsonable_encoder`` pre-serializer
    # read the wrong attribute, which manifested as every persisted trace
    # returning empty on reload.
    metadata: Optional[dict] = Field(None, validation_alias="metadata_")
    rating: Optional[str] = None
    created_at: datetime
    attachments: list[AttachmentOut] = []
    model_config = {"from_attributes": True, "populate_by_name": True}


class MessageRatingUpdate(BaseModel):
    rating: Optional[str] = Field(None, pattern="^(up|down)$")


class MessageEdit(BaseModel):
    content: str = Field(..., min_length=1)


class MessageUpdate(BaseModel):
    """In-place patch payload for ``PUT /messages/{id}``.

    Only the supplied fields are written; ``None`` leaves them untouched.
    """
    content: Optional[str] = None
    trace_id: Optional[str] = None
    latency_ms: Optional[int] = None
    model_name: Optional[str] = None
    agent_name: Optional[str] = None
    metadata: Optional[dict] = None


class ConversationOut(BaseModel):
    id: int
    title: str
    agent_id: Optional[int]
    origin: Optional[str] = None
    # Surfaced so the frontend can confirm scoping (e.g. ANILALM never
    # accepts a row whose collection_id != current workspace id).
    collection_id: Optional[int] = None
    classified: bool
    classified_at: Optional[datetime]
    # P3: TRUE when ``classified`` was set by the platform's memory
    # inheritance latch rather than by an agent's requires_encryption
    # flag or an admin's manual classify action. The UI uses this to
    # render a different banner ("此對話因引用過往加密記憶而升級為機密"
    # vs the existing "此對話為機密"). Always FALSE on rows pre-dating
    # migration 0031, so old data renders as before.
    classification_inherited: bool = False
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class ConversationDetail(ConversationOut):
    messages: list[MessageOut] = []


class MessageAppend(BaseModel):
    role: str = Field(..., pattern="^(user|assistant|system|tool)$")
    content: str
    trace_id: Optional[str] = None
    latency_ms: Optional[int] = None
    model_name: Optional[str] = None
    agent_name: Optional[str] = None
    metadata: Optional[dict] = None


class ShareCreate(BaseModel):
    mode: str = Field("read_only", pattern="^(read_only|fork)$")
    allow_fork: bool = False
    expires_at: Optional[datetime] = None


class ShareOut(BaseModel):
    id: int
    token: str
    mode: str
    allow_fork: bool
    expires_at: Optional[datetime]
    view_count: int
    created_at: datetime
    model_config = {"from_attributes": True}


# ── Conversation CRUD ─────────────────────────────────────────────────────────

@router.get("", response_model=list[ConversationOut])
def list_conversations(
    origin: Optional[str] = Query(
        default=None,
        max_length=32,
        description="Only return conversations tagged with this origin (e.g. 'anilalm').",
    ),
    exclude_origin: Optional[str] = Query(
        default=None,
        max_length=32,
        description=(
            "Return everything EXCEPT this origin. NULL-origin (legacy) "
            "rows are kept. Mutually exclusive with `origin`."
        ),
    ),
    collection_id: Optional[int] = Query(
        default=None,
        ge=1,
        description=(
            "Only return conversations scoped to this knowledge base. "
            "Required by ANILALM (cross-collection bleed otherwise); "
            "leave unset for ANILA UI (no collection concept). "
            "NULL-collection rows are NOT returned when this is set — "
            "see service docstring for rationale."
        ),
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if origin is not None and exclude_origin is not None:
        raise HTTPException(
            status_code=400,
            detail="origin and exclude_origin are mutually exclusive",
        )
    return svc.list_conversations(
        db, current_user,
        origin=origin,
        exclude_origin=exclude_origin,
        collection_id=collection_id,
    )


@router.post("", response_model=ConversationOut, status_code=201)
def create_conversation(
    body: ConversationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Contract enforcement (see ConversationCreate.collection_id docstring):
    #   - origin='anilalm' MUST set collection_id
    #   - any other origin MUST leave collection_id None
    # We treat the empty string the same as None for `origin` to stay
    # consistent with the rest of the API.
    origin = body.origin or None
    if origin == "anilalm" and body.collection_id is None:
        raise HTTPException(
            status_code=400,
            detail="anilalm conversations require a collection_id",
        )
    if origin != "anilalm" and body.collection_id is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                "collection_id is only valid for origin='anilalm' "
                "(other origins have no knowledge-base scope)"
            ),
        )
    # If a collection_id was supplied, verify the user actually owns /
    # has access to that collection so we don't end up creating a
    # conversation pinned to someone else's knowledge base. Lazy import
    # to avoid a circular dependency between conversations and ingestion.
    if body.collection_id is not None:
        from app.api.ingestion.collections import _require_collection_access
        _require_collection_access(db, current_user, body.collection_id)
    return svc.create_conversation(
        db,
        current_user.id,
        title=body.title,
        agent_id=body.agent_id,
        origin=origin,
        collection_id=body.collection_id,
    )


@router.get("/{conv_id}", response_model=ConversationDetail)
def get_conversation(
    conv_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conv = svc.get_conversation(db, conv_id, current_user)
    if conv.classified:
        svc.log_classified_access(db, conv_id, current_user)
    return conv


@router.put("/{conv_id}", response_model=ConversationOut)
def update_conversation(
    conv_id: int,
    body: ConversationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return svc.update_title(db, conv_id, body.title, current_user)


@router.delete("/{conv_id}", status_code=204)
def delete_conversation(
    conv_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc.delete_conversation(db, conv_id, current_user)


# ── Messages ──────────────────────────────────────────────────────────────────

@router.post("/{conv_id}/messages", response_model=MessageOut, status_code=201)
def append_message(
    conv_id: int,
    body: MessageAppend,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return svc.append_message(
        db, conv_id, current_user,
        role=body.role,
        content=body.content,
        trace_id=body.trace_id,
        latency_ms=body.latency_ms,
        model_name=body.model_name,
        agent_name=body.agent_name,
        metadata=body.metadata,
    )


@router.put("/{conv_id}/messages/{message_id}/rating", response_model=MessageOut)
def set_message_rating(
    conv_id: int,
    message_id: int,
    body: MessageRatingUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Record thumbs-up/down on an assistant message, or clear with rating=null."""
    return svc.set_message_rating(db, conv_id, message_id, current_user, body.rating)


@router.put("/{conv_id}/messages/{message_id}", response_model=MessageOut)
def update_message(
    conv_id: int,
    message_id: int,
    body: MessageUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Patch an existing message (in-place, non-truncating).

    Primarily used by the assistant regenerate flow to replace the old reply
    without piling up orphan assistant rows in the DB.
    """
    return svc.update_message_content(
        db, conv_id, message_id, current_user,
        content=body.content,
        trace_id=body.trace_id,
        latency_ms=body.latency_ms,
        model_name=body.model_name,
        agent_name=body.agent_name,
        metadata=body.metadata,
    )


@router.put("/{conv_id}/messages/{message_id}/edit", response_model=MessageOut)
def edit_user_message(
    conv_id: int,
    message_id: int,
    body: MessageEdit,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Rewrite a user message and drop everything after it.

    The caller is expected to immediately re-send the chat turn with the new
    content; returning the updated message lets the UI reconcile dbId / rating
    without a separate fetch.
    """
    return svc.edit_user_message(db, conv_id, message_id, current_user, body.content)


# ── Classified policy ─────────────────────────────────────────────────────────
#
# Classified is one-way (README, Wave 2). The platform's invariant is
# "once latched, never downgraded" — Sprint 8 X / Phase K removes the
# /declassify endpoint entirely (was a pre-existing backdoor that
# violated the invariant; nothing in the frontend ever called it).
# To handle a genuine misclassification, ops should write a manual
# admin script + audit entry rather than expose a downgrade HTTP
# surface.

@router.post("/{conv_id}/classify", response_model=ConversationOut)
def classify_conversation(
    conv_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return svc.classify_conversation(db, conv_id, current_user)


# ── Share links ───────────────────────────────────────────────────────────────

@router.get("/{conv_id}/shares", response_model=list[ShareOut])
def list_shares(
    conv_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return svc.list_shares(db, conv_id, current_user)


@router.post("/{conv_id}/shares", response_model=ShareOut, status_code=201)
def create_share(
    conv_id: int,
    body: ShareCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return svc.create_share(
        db, conv_id, current_user,
        mode=body.mode,
        allow_fork=body.allow_fork,
        expires_at=body.expires_at,
    )


@router.delete("/{conv_id}/shares/{share_id}", status_code=204)
def revoke_share(
    conv_id: int,
    share_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc.revoke_share(db, share_id, current_user)
