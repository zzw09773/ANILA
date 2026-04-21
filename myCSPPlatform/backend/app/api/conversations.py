"""Conversation management endpoints (JWT auth)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
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
    metadata: Optional[dict] = Field(None, alias="metadata_")
    created_at: datetime
    attachments: list[AttachmentOut] = []
    model_config = {"from_attributes": True, "populate_by_name": True}


class ConversationOut(BaseModel):
    id: int
    title: str
    agent_id: Optional[int]
    classified: bool
    classified_at: Optional[datetime]
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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return svc.list_conversations(db, current_user)


@router.post("", response_model=ConversationOut, status_code=201)
def create_conversation(
    body: ConversationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return svc.create_conversation(db, current_user.id, title=body.title, agent_id=body.agent_id)


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


# ── Classified policy ─────────────────────────────────────────────────────────

@router.post("/{conv_id}/classify", response_model=ConversationOut)
def classify_conversation(
    conv_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return svc.classify_conversation(db, conv_id, current_user)


@router.post("/{conv_id}/declassify", response_model=ConversationOut)
def declassify_conversation(
    conv_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return svc.declassify_conversation(db, conv_id, current_user)


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
