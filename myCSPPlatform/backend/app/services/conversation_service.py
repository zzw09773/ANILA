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
) -> Conversation:
    conv = Conversation(user_id=user_id, title=title, agent_id=agent_id)
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


def list_conversations(db: Session, user: User) -> list[Conversation]:
    return (
        db.query(Conversation)
        .filter(Conversation.user_id == user.id)
        .order_by(Conversation.updated_at.desc())
        .all()
    )


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
