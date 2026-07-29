"""Conversation persistence and share-link service."""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.config import settings
from app.models.attachment import Attachment
from app.models.audit_log import AuditLog
from app.models.conversation import Conversation, ConversationShare
from app.models.message import Message
from app.models.user import User
from app.services.audit_service import log_audit_event
from app.services.auth_service import is_admin_tier
from app.services import message_tree as mtree


# Client-supplied message metadata is an opaque dict persisted verbatim
# (trace_id, latency, model/agent name, structured feedback). Cap its
# serialized size so an authenticated insider can't bloat a row; 64KB is far
# above any legitimate metadata payload.
_MAX_METADATA_BYTES = 64 * 1024


def _check_metadata_size(metadata: Optional[dict]) -> None:
    if metadata is None:
        return
    try:
        size = len(json.dumps(metadata, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail="metadata 不是合法的 JSON 物件"
        ) from exc
    if size > _MAX_METADATA_BYTES:
        raise HTTPException(status_code=413, detail="metadata 過大")


def _max_siblings() -> int:
    return int(settings.ANILA_MESSAGE_MAX_SIBLINGS)


def _require_branchable(conv: Conversation) -> None:
    """ANILALM conversations do not support message branching (SYSTEM-MAP:47).

    NULL origin = legacy ANILA (same semantics as list_conversations exclude).
    """
    if conv.origin == "anilalm":
        raise HTTPException(
            status_code=409,
            detail="ANILALM 對話不支援訊息分支",
        )


def _sibling_count(db: Session, conversation_id: int, parent_id: int | None) -> int:
    q = db.query(Message).filter(Message.conversation_id == conversation_id)
    if parent_id is None:
        q = q.filter(Message.parent_id.is_(None))
    else:
        q = q.filter(Message.parent_id == parent_id)
    return q.count()


def _enforce_sibling_cap(
    db: Session, conversation_id: int, parent_id: int | None,
) -> None:
    cap = _max_siblings()
    if _sibling_count(db, conversation_id, parent_id) >= cap:
        raise HTTPException(
            status_code=409,
            detail=f"同一則訊息的變體已達上限（{cap}）",
        )


def _enforce_explicit_parent_role(
    db: Session,
    conversation_id: int,
    parent_id: int | None,
    role: str,
) -> None:
    """When forking under an explicit parent that already has children, roles must match."""
    q = db.query(Message).filter(Message.conversation_id == conversation_id)
    if parent_id is None:
        q = q.filter(Message.parent_id.is_(None))
    else:
        q = q.filter(Message.parent_id == parent_id)
    existing = q.first()
    if existing is not None and existing.role != role:
        raise HTTPException(
            status_code=400,
            detail="分支訊息的角色必須與既有子訊息相同",
        )


def _resolve_parent_id(
    db: Session,
    conv: Conversation,
    parent_id: int | None,
    *,
    explicit: bool,
) -> int | None:
    """Resolve parent for append.

    ``explicit=False`` (omitted/null): default to active leaf.
    ``explicit=True``: validate the supplied id belongs to this conversation.
    """
    if not explicit:
        return conv.active_leaf_message_id
    parent = db.query(Message).filter(Message.id == parent_id).first()
    if parent is None:
        raise HTTPException(status_code=400, detail="父訊息不存在")
    if parent.conversation_id != conv.id:
        raise HTTPException(status_code=400, detail="父訊息不屬於此對話")
    return parent.id


def _lock_conversation(db: Session, conv_id: int) -> Conversation:
    """Row-lock the conversation (FOR UPDATE; no-op on SQLite).

    ``populate_existing`` forces a reload so the identity-map object
    reflects the committed row (plain ``with_for_update`` alone does not).
    """
    return (
        db.query(Conversation)
        .filter(Conversation.id == conv_id)
        .populate_existing()
        .with_for_update()
        .one()
    )


def load_active_path(db: Session, conv: Conversation) -> list[Message]:
    """Hydrate the active root→leaf path.

    If the pointer is NULL or dangling but message rows exist, degrade to
    the full flat list ordered ``(created_at, id)`` (never empty). Persist
    a healed pointer to the newest row only when walking that leaf recovers
    every message id (chain intact). Un-backfilled flat rows keep the NULL
    pointer so the diagnostic signal remains visible.
    """
    edges = mtree.load_edges(db, conv.id)
    path_ids = mtree.active_path_ids(edges, conv.active_leaf_message_id)
    if path_ids:
        by_id = {
            m.id: m
            for m in db.query(Message)
            .filter(Message.id.in_(path_ids))
            .all()
        }
        return [by_id[i] for i in path_ids if i in by_id]

    all_rows = _all_messages_ordered(db, conv.id)
    if not all_rows:
        return []
    # Pointer NULL/dangling — flat fallback; heal only if chain is intact.
    newest_id = all_rows[-1].id
    recovered = mtree.active_path_ids(edges, newest_id)
    if len(recovered) == len(all_rows) and set(recovered) == {m.id for m in all_rows}:
        conv.active_leaf_message_id = newest_id
        db.commit()
    return all_rows


# Thin alias for internal callers still using the underscore name.
_load_active_path = load_active_path


def _all_messages_ordered(db: Session, conversation_id: int) -> list[Message]:
    return (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.created_at, Message.id)
        .all()
    )


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
    """Delete a conversation and all of its messages.

    OW-1: self-FK on messages.parent_id and conversations.active_leaf_message_id
    break per-row ORM delete. Null the pointer → flush → null attachment
    message_ids → bulk delete messages → bulk delete attachments → delete conv.
    (SQLite tests do not fire DB CASCADE; Python must own the cleanup.)
    docs/plans/ow1-message-tree-blueprint.md Q4.
    """
    conv = get_conversation(db, conv_id, user)
    conv.active_leaf_message_id = None
    db.flush()
    (
        db.query(Attachment)
        .filter(Attachment.conversation_id == conv.id)
        .update({Attachment.message_id: None}, synchronize_session=False)
    )
    (
        db.query(Message)
        .filter(Message.conversation_id == conv.id)
        .delete(synchronize_session=False)
    )
    (
        db.query(Attachment)
        .filter(Attachment.conversation_id == conv.id)
        .delete(synchronize_session=False)
    )
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
    parent_id: Optional[int] = None,
    parent_id_explicit: bool = False,
    set_active: bool = True,
) -> Message:
    """Append a message, threading onto the active leaf by default (OW-1).

    When ``parent_id_explicit`` is True the caller supplied a parent id
    (ANILALM branching is rejected). Omitted/null parent defaults to the
    current ``active_leaf_message_id``.
    """
    _check_metadata_size(metadata)
    conv = get_conversation(db, conv_id, user)
    # Serialize concurrent appends so sibling cap / parent resolve cannot race.
    conv = _lock_conversation(db, conv.id)
    if parent_id_explicit:
        _require_branchable(conv)
    resolved_parent = _resolve_parent_id(
        db, conv, parent_id, explicit=parent_id_explicit,
    )
    if parent_id_explicit:
        _enforce_explicit_parent_role(db, conv.id, resolved_parent, role)
    _enforce_sibling_cap(db, conv.id, resolved_parent)
    msg = Message(
        conversation_id=conv.id,
        parent_id=resolved_parent,
        role=role,
        content=content,
        trace_id=trace_id,
        latency_ms=latency_ms,
        model_name=model_name,
        agent_name=agent_name,
        metadata_=metadata,
    )
    db.add(msg)
    db.flush()
    # set_active=false only declines to MOVE an existing pointer; never leave NULL.
    if set_active or conv.active_leaf_message_id is None:
        conv.active_leaf_message_id = msg.id
    conv.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(msg)
    return msg


def branch_message(
    db: Session,
    conv_id: int,
    message_id: int,
    user: User,
    *,
    role: str,
    content: str,
    trace_id: Optional[str] = None,
    latency_ms: Optional[int] = None,
    model_name: Optional[str] = None,
    agent_name: Optional[str] = None,
    metadata: Optional[dict] = None,
    set_active: bool = True,
) -> Message:
    """Create a sibling of ``message_id`` (edit-re-ask and regenerate).

    Server sets ``parent_id = target.parent_id``. Role must match the target.
    docs/plans/ow1-message-tree-blueprint.md Q2/Q3/Q5.
    """
    _check_metadata_size(metadata)
    conv = get_conversation(db, conv_id, user)
    # Serialize concurrent branches so sibling cap / pointer cannot race.
    conv = _lock_conversation(db, conv.id)
    _require_branchable(conv)
    target = (
        db.query(Message)
        .filter(Message.id == message_id, Message.conversation_id == conv.id)
        .first()
    )
    if target is None:
        raise HTTPException(status_code=404, detail="訊息不存在")
    if target.role not in ("user", "assistant"):
        raise HTTPException(
            status_code=400,
            detail="不可從 system/tool 訊息建立分支",
        )
    if role != target.role:
        raise HTTPException(
            status_code=400,
            detail="分支訊息的角色必須與原訊息相同",
        )
    parent = target.parent_id
    _enforce_sibling_cap(db, conv.id, parent)
    msg = Message(
        conversation_id=conv.id,
        parent_id=parent,
        role=role,
        content=content,
        trace_id=trace_id,
        latency_ms=latency_ms,
        model_name=model_name,
        agent_name=agent_name,
        metadata_=metadata,
    )
    db.add(msg)
    db.flush()
    # set_active=false only declines to MOVE an existing pointer; never leave NULL.
    if set_active or conv.active_leaf_message_id is None:
        conv.active_leaf_message_id = msg.id
    conv.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(msg)
    return msg


def set_active_leaf(
    db: Session,
    conv_id: int,
    user: User,
    message_id: int,
) -> tuple[Conversation, list[Message]]:
    """Point active leaf at ``message_id``, canonicalizing to newest descendant."""
    conv = get_conversation(db, conv_id, user)
    conv = _lock_conversation(db, conv.id)
    _require_branchable(conv)
    target = (
        db.query(Message)
        .filter(Message.id == message_id, Message.conversation_id == conv.id)
        .first()
    )
    if target is None:
        raise HTTPException(status_code=404, detail="訊息不存在")
    edges = mtree.load_edges(db, conv.id)
    children = mtree.children_map(edges)
    leaf_id = mtree.newest_leaf_under(children, target.id)
    conv.active_leaf_message_id = leaf_id
    conv.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(conv)
    return conv, load_active_path(db, conv)


def delete_message_branch(
    db: Session,
    conv_id: int,
    message_id: int,
    user: User,
) -> tuple[Conversation, list[Message]]:
    """Subtree-delete ``message_id`` and descendants (Python-side; SQLite-safe).

    docs/plans/ow1-message-tree-blueprint.md Q4.
    """
    conv = get_conversation(db, conv_id, user)
    conv = _lock_conversation(db, conv.id)
    _require_branchable(conv)
    target = (
        db.query(Message)
        .filter(Message.id == message_id, Message.conversation_id == conv.id)
        .first()
    )
    if target is None:
        raise HTTPException(status_code=404, detail="訊息不存在")

    edges = mtree.load_edges(db, conv.id)
    children = mtree.children_map(edges)
    subtree = mtree.descendants(children, target.id, include_self=True)
    surviving = {eid for eid, _ in edges} - subtree
    if not surviving:
        raise HTTPException(
            status_code=409,
            detail="對話至少需保留一則訊息;請改為刪除整個對話",
        )

    # Repoint active leaf before delete if it sits inside the subtree.
    if conv.active_leaf_message_id in subtree:
        parent_id = target.parent_id
        if parent_id is not None and parent_id in surviving:
            start = parent_id
        else:
            # Newest surviving root (roots are ordered in children[None]).
            roots = [r for r in children.get(None, []) if r in surviving]
            if not roots:
                # Defensive: surviving non-roots only (shouldn't happen).
                start = max(surviving)
            else:
                start = roots[-1]
        # Rebuild children map excluding subtree so descend stays on survivors.
        surviving_edges = [(i, p) for i, p in edges if i in surviving]
        surviving_children = mtree.children_map(surviving_edges)
        conv.active_leaf_message_id = mtree.newest_leaf_under(
            surviving_children, start,
        )

    deleted_ids = sorted(subtree)
    (
        db.query(Attachment)
        .filter(Attachment.message_id.in_(deleted_ids))
        .update({Attachment.message_id: None}, synchronize_session=False)
    )
    (
        db.query(Message)
        .filter(Message.id.in_(deleted_ids))
        .delete(synchronize_session=False)
    )
    log_audit_event(
        db,
        action="delete_message_branch",
        resource_type="conversation",
        actor=user,
        resource_id=conv.id,
        detail=(
            f"User {user.username} deleted message branch "
            f"({len(deleted_ids)} messages) in conversation {conv.id}"
        ),
        metadata={"deleted_ids": deleted_ids, "count": len(deleted_ids)},
    )
    conv.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(conv)
    return conv, load_active_path(db, conv)


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
    """In-place patch of an existing message (metadata / ANILALM finalize).

    OW-1: ANILA regenerate no longer uses this path — it creates an assistant
    sibling via ``branch_message``. This endpoint remains for non-forking
    patches (ANILALM finalize, metadata updates).
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
        _check_metadata_size(metadata)
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
    comment: Optional[str] = None,
    reasons: Optional[list] = None,
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
    # Structured feedback rides in metadata_['feedback']. air-gap 環境下這是
    # 平台團隊評估模型品質的主要訊號。清除評分時一併清掉回饋。
    meta = dict(msg.metadata_ or {})
    if rating is None:
        meta.pop("feedback", None)
    elif comment or reasons:
        meta["feedback"] = {
            "comment": (comment or "").strip() or None,
            "reasons": reasons or [],
        }
    msg.metadata_ = meta or None
    db.commit()
    db.refresh(msg)
    return msg


# ── Classified policy ─────────────────────────────────────────────────────────

def classify_conversation(db: Session, conv_id: int, user: User) -> Conversation:
    """Mark conversation as classified (irreversible by non-admin).

    Slice 3b: routes through the four-level one-way core (SYSTEM-MAP §8)
    (``apply_classification`` reason=``manual_admin``) which writes the
    ClassificationEvent, sets ``classification_level`` to
    :attr:`ClassificationLevel.RESTRICTED` (``密``; the mirror floor for
    ``classified=True``) and mirrors the legacy boolean. ``classified_by``
    is an admin-attribution field the event model doesn't carry, so it's
    stamped here alongside the existing AuditLog trail.
    """
    from app.schemas.contracts.classification import ClassificationLevel

    conv = get_conversation(db, conv_id, user)
    # Idempotency on the level (OE-4): boolean is display-only; already at
    # RESTRICTED+ means the manual classify floor is already applied.
    level = ClassificationLevel.from_storage(conv.classification_level)
    if level >= ClassificationLevel.RESTRICTED:
        raise HTTPException(status_code=409, detail="此對話已標示為機敏")
    from app.modules.policy import apply_classification
    apply_classification(
        db,
        resource_type="conversation",
        resource_id=str(conv_id),
        new_level=ClassificationLevel.RESTRICTED.to_storage(),
        actor_type="user",
        actor_id=str(user.id),
        reason="manual_admin",
        source="manual_admin",
    )
    conv.classified_by = user.id
    # Supplementary AuditLog trail (the authoritative record is the
    # ClassificationEvent written by apply_classification above). Uses the
    # blessed helper — the previous hand-built AuditLog(user_id=..., details=)
    # used column names the model doesn't have (actor_user_id / detail), so
    # the manual classify path 500'd before this fix.
    log_audit_event(
        db,
        action="classify_conversation",
        resource_type="conversation",
        actor=user,
        resource_id=conv_id,
        detail=f"User {user.username} classified conversation {conv_id}",
    )
    db.commit()
    db.refresh(conv)
    return conv


# Sprint 8 X / Phase K — declassify_conversation removed. Classified
# is one-way per platform invariant. The previous implementation was a
# backdoor that the README explicitly contradicted; the corresponding
# HTTP route in app/api/conversations.py is also gone.


def log_classified_access(db: Session, conv_id: int, user: User) -> None:
    # Field names match the AuditLog model exactly: actor_user_id /
    # actor_username / detail (singular). Trigger predicate lives at the
    # call site (level >= TRADE_SECRET; SYSTEM-MAP §8 L242) — OE-4 does
    # not read conversations.classified for audit decisions.
    db.add(AuditLog(
        actor_user_id=user.id,
        actor_username=user.username,
        action="access_classified_conversation",
        resource_type="conversation",
        resource_id=str(conv_id),
        status="success",
        detail=f"User {user.username} accessed classified conversation {conv_id}",
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
    from app.schemas.contracts.classification import (
        ClassificationLevel,
        classification_audit_required,
        outbound_action_allowed,
    )

    conv = get_conversation(db, conv_id, user)
    level = ClassificationLevel.from_storage(conv.classification_level)
    # SYSTEM-MAP §8 L241-242: allow iff level <= TRADE_SECRET; audit iff
    # level >= TRADE_SECRET (including the allow path for 營業秘密).
    if not outbound_action_allowed(level):
        raise HTTPException(
            status_code=403,
            detail="列管對話不允許建立分享連結",
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
    if classification_audit_required(level):
        log_audit_event(
            db,
            action="share_conversation",
            resource_type="conversation",
            actor=user,
            resource_id=conv_id,
            detail=(
                f"User {user.username} shared conversation {conv_id} "
                f"at level {level.to_storage()}"
            ),
            metadata={"classification_level": level.to_storage()},
        )
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
    if is_admin_tier(user):
        return
    if conv.user_id != user.id:
        raise HTTPException(status_code=403, detail="無權存取此對話")
