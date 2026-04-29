"""Conversation persistence and share-link service."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.conversation import Conversation, ConversationShare
from app.models.message import Message
from app.models.user import User


# ── Conversation CRUD ─────────────────────────────────────────────────────────

def create_conversation(
    db: Session,
    user_id: int,
    title: str = "新對話",
    agent_id: Optional[int] = None,
    origin: Optional[str] = None,
    collection_id: Optional[int] = None,
) -> Conversation:
    """Create a conversation row.

    `collection_id` is only meaningful for `origin='anilalm'` (knowledge-
    base scoping). The API layer enforces the contract that anilalm
    conversations MUST set collection_id; other origins MUST leave it
    None. This function trusts whatever the caller passed and just
    persists it.
    """
    conv = Conversation(
        user_id=user_id,
        title=title,
        agent_id=agent_id,
        origin=origin,
        collection_id=collection_id,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def get_conversation(db: Session, conv_id: int, user: User) -> Conversation:
    conv = db.query(Conversation).filter(Conversation.id == conv_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="找不到此對話")
    _check_access(conv, user)
    return conv


def list_conversations(
    db: Session,
    user: User,
    origin: Optional[str] = None,
    exclude_origin: Optional[str] = None,
    collection_id: Optional[int] = None,
) -> list[Conversation]:
    """List the caller's conversations, optionally filtered.

    Origin filtering modes (mutually exclusive — endpoint validates this):
      - ``origin='anilalm'``           → only LM-side conversations.
      - ``exclude_origin='anilalm'``   → everything except LM-side; this
        is what ANILA UI uses so its sidebar doesn't see retrieval Q&A.
        ``NULL`` rows (pre-migration legacy) are kept in the result so
        users don't lose their pre-existing chat history.
      - both NULL                      → all the user's conversations.

    Collection scoping (independent from origin filters):
      - ``collection_id=N``            → only conversations scoped to that
        knowledge base. Required by ANILALM so its sidebar shows just
        the open knowledge base's chats.
      - omitted                        → don't constrain on collection;
        legacy / cross-origin lists work as before.

    The collection filter does NOT include NULL-collection rows, even
    though origin's exclude mode does. Reason: a NULL-collection row is
    unambiguously "not scoped to any knowledge base"; pretending it
    belongs to whichever collection the user is currently viewing would
    re-introduce the cross-collection leak this filter exists to fix.
    """
    q = db.query(Conversation).filter(Conversation.user_id == user.id)
    if origin is not None:
        q = q.filter(Conversation.origin == origin)
    elif exclude_origin is not None:
        q = q.filter(
            (Conversation.origin.is_(None)) | (Conversation.origin != exclude_origin)
        )
    if collection_id is not None:
        q = q.filter(Conversation.collection_id == collection_id)
    return q.order_by(Conversation.updated_at.desc()).all()


def update_title(db: Session, conv_id: int, title: str, user: User) -> Conversation:
    conv = get_conversation(db, conv_id, user)
    conv.title = title
    conv.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(conv)
    return conv


def delete_conversation(db: Session, conv_id: int, user: User) -> None:
    conv = get_conversation(db, conv_id, user)
    db.delete(conv)
    db.commit()


# ── Message persistence ───────────────────────────────────────────────────────

def append_message(
    db: Session,
    conv_id: int,
    user: User,
    *,
    role: str,
    content: str,
    trace_id: Optional[str] = None,
    latency_ms: Optional[int] = None,
    model_name: Optional[str] = None,
    agent_name: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> Message:
    conv = get_conversation(db, conv_id, user)
    msg = Message(
        conversation_id=conv.id,
        role=role,
        content=content,
        trace_id=trace_id,
        latency_ms=latency_ms,
        model_name=model_name,
        agent_name=agent_name,
        metadata_=metadata,
    )
    db.add(msg)
    conv.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(msg)
    return msg


def edit_user_message(
    db: Session,
    conv_id: int,
    message_id: int,
    user: User,
    content: str,
) -> Message:
    """Rewrite a user message's content and drop every message after it.

    Used by the ANILA UI "edit" action: user revises a prompt, the UI then
    re-sends it. Trailing messages (original assistant reply plus any further
    turns) become stale the moment the edit lands, so we delete them in the
    same transaction — otherwise a reload would show the new user message
    followed by an orphaned, out-of-date assistant reply.
    """
    conv = get_conversation(db, conv_id, user)
    msg = (
        db.query(Message)
        .filter(Message.id == message_id, Message.conversation_id == conv.id)
        .first()
    )
    if msg is None:
        raise HTTPException(status_code=404, detail="訊息不存在")
    if msg.role != "user":
        raise HTTPException(status_code=400, detail="僅使用者訊息可編輯")
    msg.content = content
    (
        db.query(Message)
        .filter(
            Message.conversation_id == conv.id,
            Message.created_at > msg.created_at,
        )
        .delete(synchronize_session=False)
    )
    conv.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(msg)
    return msg


def update_message_content(
    db: Session,
    conv_id: int,
    message_id: int,
    user: User,
    *,
    content: Optional[str] = None,
    trace_id: Optional[str] = None,
    latency_ms: Optional[int] = None,
    model_name: Optional[str] = None,
    agent_name: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> Message:
    """In-place update of an existing message (used by Regenerate).

    Unlike ``edit_user_message`` (which rewrites a user message and drops
    every later turn), this path is non-truncating — it's meant for the
    "regenerate assistant reply" flow where we want to *replace* the body
    of the same row, keeping created_at / rating / id stable so reload
    order and sidebar labels don't drift.
    """
    conv = get_conversation(db, conv_id, user)
    msg = (
        db.query(Message)
        .filter(Message.id == message_id, Message.conversation_id == conv.id)
        .first()
    )
    if msg is None:
        raise HTTPException(status_code=404, detail="訊息不存在")
    if content is not None:
        msg.content = content
    if trace_id is not None:
        msg.trace_id = trace_id
    if latency_ms is not None:
        msg.latency_ms = latency_ms
    if model_name is not None:
        msg.model_name = model_name
    if agent_name is not None:
        msg.agent_name = agent_name
    if metadata is not None:
        msg.metadata_ = metadata
    conv.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(msg)
    return msg


def set_message_rating(
    db: Session,
    conv_id: int,
    message_id: int,
    user: User,
    rating: Optional[str],
) -> Message:
    """Record thumbs-up/down feedback on an assistant message.

    Access is gated by conversation ownership (reusing get_conversation's
    _check_access). Only assistant messages are ratable — rating a user/system
    message is a client bug and 400s out. ``rating=None`` clears an existing
    rating, letting the UI toggle off.
    """
    conv = get_conversation(db, conv_id, user)
    msg = (
        db.query(Message)
        .filter(Message.id == message_id, Message.conversation_id == conv.id)
        .first()
    )
    if msg is None:
        raise HTTPException(status_code=404, detail="訊息不存在")
    if msg.role != "assistant":
        raise HTTPException(status_code=400, detail="僅助理訊息可評分")
    msg.rating = rating
    db.commit()
    db.refresh(msg)
    return msg


# ── Classified policy ─────────────────────────────────────────────────────────

def classify_conversation(db: Session, conv_id: int, user: User) -> Conversation:
    """Mark conversation as classified (irreversible by non-admin)."""
    conv = get_conversation(db, conv_id, user)
    if conv.classified:
        raise HTTPException(status_code=409, detail="此對話已標示為機密")
    conv.classified = True
    conv.classified_at = datetime.now(timezone.utc)
    conv.classified_by = user.id
    db.add(AuditLog(
        user_id=user.id,
        action="classify_conversation",
        resource_type="conversation",
        resource_id=str(conv_id),
        status="success",
        details=f"User {user.username} classified conversation {conv_id}",
    ))
    db.commit()
    db.refresh(conv)
    return conv


def declassify_conversation(db: Session, conv_id: int, admin: User) -> Conversation:
    """Remove classified status — admin only."""
    if admin.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理員權限")
    conv = db.query(Conversation).filter(Conversation.id == conv_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="找不到此對話")
    conv.classified = False
    conv.classified_at = None
    conv.classified_by = None
    db.add(AuditLog(
        user_id=admin.id,
        action="declassify_conversation",
        resource_type="conversation",
        resource_id=str(conv_id),
        status="success",
        details=f"Admin {admin.username} declassified conversation {conv_id}",
    ))
    db.commit()
    db.refresh(conv)
    return conv


def log_classified_access(db: Session, conv_id: int, user: User) -> None:
    db.add(AuditLog(
        user_id=user.id,
        action="access_classified_conversation",
        resource_type="conversation",
        resource_id=str(conv_id),
        status="success",
        details=f"User {user.username} accessed classified conversation {conv_id}",
    ))
    db.commit()


# ── Share links ───────────────────────────────────────────────────────────────

def create_share(
    db: Session,
    conv_id: int,
    user: User,
    *,
    mode: str = "read_only",
    allow_fork: bool = False,
    expires_at: Optional[datetime] = None,
) -> ConversationShare:
    conv = get_conversation(db, conv_id, user)
    if conv.classified:
        raise HTTPException(
            status_code=403,
            detail="機密對話不允許建立分享連結",
        )
    share = ConversationShare(
        conversation_id=conv.id,
        token=secrets.token_urlsafe(32),
        mode=mode,
        allow_fork=allow_fork,
        expires_at=expires_at,
        created_by=user.id,
    )
    db.add(share)
    db.commit()
    db.refresh(share)
    return share


def get_share_by_token(db: Session, token: str) -> ConversationShare:
    share = (
        db.query(ConversationShare)
        .filter(ConversationShare.token == token)
        .first()
    )
    if not share:
        raise HTTPException(status_code=404, detail="找不到此分享連結")
    if share.expires_at and share.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="此分享連結已過期")
    share.view_count += 1
    db.commit()
    return share


def list_shares(db: Session, conv_id: int, user: User) -> list[ConversationShare]:
    conv = get_conversation(db, conv_id, user)
    return conv.shares


def revoke_share(db: Session, share_id: int, user: User) -> None:
    share = db.query(ConversationShare).filter(ConversationShare.id == share_id).first()
    if not share:
        raise HTTPException(status_code=404, detail="找不到此分享連結")
    conv = get_conversation(db, share.conversation_id, user)  # ownership check
    db.delete(share)
    db.commit()


# ── Internal helpers ──────────────────────────────────────────────────────────

def _check_access(conv: Conversation, user: User) -> None:
    if user.role == "admin":
        return
    if conv.user_id != user.id:
        raise HTTPException(status_code=403, detail="無權存取此對話")
