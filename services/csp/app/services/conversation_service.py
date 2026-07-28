"""Conversation persistence and share-link service."""
from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.conversation import Conversation, ConversationShare
from app.models.message import Message
from app.models.user import User
from anila_contracts import Classification as ClassificationLevel
from app.services.audit_service import log_audit_event
from app.services.auth_service import is_admin_tier

# C3 §c: non-active branch retention (days). Unset / empty = same as main chain
# (retain forever relative to the conversation unit). config.py is out of scope
# for W2-3, so this is read from the environment directly.
_BRANCH_RETENTION_ENV = "ANILA_MESSAGE_BRANCH_RETENTION_DAYS"


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
    rows, _ = list_conversations_page(
        db,
        user,
        origin=origin,
        exclude_origin=exclude_origin,
        collection_id=collection_id,
        limit=None,
        cursor=None,
    )
    return rows


def list_conversations_page(
    db: Session,
    user: User,
    *,
    origin: Optional[str] = None,
    exclude_origin: Optional[str] = None,
    collection_id: Optional[int] = None,
    limit: Optional[int] = None,
    cursor: Optional[int] = None,
) -> tuple[list[Conversation], Optional[int]]:
    """回傳 `(對話列, 下一頁 cursor)`。`limit=None` 就是舊行為(全撈)。

    W3-7e。先前是 `.order_by(updated_at.desc()).all()` —— 沒有分頁,一個累積了
    五千條對話的使用者每次側欄載入都把五千列連同 `updated_at`/`title` 全撈回來。

    ## 為什麼 cursor 是「最後一列的 id」而不是把時間編進去

    keyset 分頁需要一個**全序**;`updated_at` 單獨用會在同秒撞的列上重複或漏掉,
    所以排序鍵是 `(updated_at DESC, id DESC)`。但如果把 `updated_at` 的字面值編進
    cursor,就得在比較時把它 parse 回 datetime —— 而 `conversations.updated_at`
    現在是 **naive**,W2-10 之後會變 timestamptz。那個轉換一發生,「parse 出來的
    aware 值」對「naive 欄位」的比較就會 TypeError(W1-4 修的正是同一個坑)。

    所以 cursor 只帶 id,錨點的 `updated_at` 用**子查詢**現查。比較因此是
    欄位對欄位、同型別,不管那個欄位哪天變成 timestamptz 都成立。

    cursor 用裸 id 而不是 base64 包裝:client 本來就有這些 id,包起來只是假裝不透明。

    ## 為什麼回傳 tuple 而不是換 response 形狀

    API 端把 cursor 放進 `X-Next-Cursor` 回應頭,`response_model` 與 JSON 形狀
    **一個字都不變** —— 舊 client 完全不受影響,OpenAPI 產出物也不用改。
    「有參數就換 shape」那種做法會讓契約有兩個版本。
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

    if cursor is not None:
        anchor = (
            db.query(Conversation.updated_at)
            .filter(
                Conversation.id == cursor,
                # cursor 也要綁使用者:否則傳別人的 conversation id 就能把它的
                # updated_at 當錨點,間接問出「那條對話最後更新時間落在哪個區間」。
                Conversation.user_id == user.id,
            )
            .scalar_subquery()
        )
        q = q.filter(
            (Conversation.updated_at < anchor)
            | ((Conversation.updated_at == anchor) & (Conversation.id < cursor))
        )

    q = q.order_by(Conversation.updated_at.desc(), Conversation.id.desc())

    if limit is None:
        return q.all(), None

    # 多取一列來判斷「還有下一頁嗎」,避免另外跑一次 count(對大表是白付的掃描)。
    rows = q.limit(limit + 1).all()
    if len(rows) > limit:
        page = rows[:limit]
        return page, page[-1].id
    return rows, None


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


# ── Message tree helpers (W2-3 / C3) ──────────────────────────────────────────

def conversation_is_under_legal_hold(conv: Conversation) -> bool:
    """Reaper / edit-path guard: legal hold applies to the WHOLE tree.

    Follow-up (not wired yet): ``retention_reaper.py`` still only handles
    artifact/document filesystem retention; conversation message-tree
    reaping should call this helper when that work package lands.
    """
    return bool(getattr(conv, "legal_hold", False))


def branch_retention_days() -> Optional[int]:
    """Non-active-branch retention policy (C3 §c).

    ``None`` means "same as main chain" (default). A dedicated admin branch
    deletion endpoint is out of scope; this value is the reaper guard input.

    Follow-up (not wired yet): ``retention_reaper.py`` does not read this;
    wire it when message-branch reaping is implemented.
    """
    raw = (os.environ.get(_BRANCH_RETENTION_ENV) or "").strip()
    if not raw:
        return None
    try:
        days = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"{_BRANCH_RETENTION_ENV} must be an integer day count, got {raw!r}"
        ) from exc
    if days < 0:
        raise ValueError(f"{_BRANCH_RETENTION_ENV} must be >= 0, got {days}")
    return days


def message_is_on_active_path(db: Session, conv: Conversation, message_id: int) -> bool:
    """Whether ``message_id`` lies on the conversation's active path."""
    return any(m.id == message_id for m in resolve_active_path(db, conv))


def reaper_may_consider_branch_message(
    db: Session, conv: Conversation, msg: Message
) -> bool:
    """Guard for a future retention reaper (C3 §c).

    - Legal hold → never reap any node in the tree.
    - Active-path nodes → never reap via branch policy (conversation unit).
    - Off-path nodes → eligible only when ``ANILA_MESSAGE_BRANCH_RETENTION_DAYS``
      is set; default (unset) treats them like the main chain (not eligible
      for branch-only deletion). Branch deletion remains an explicit
      admin+audit operation — editing never triggers it.

    Follow-up (not wired yet): ``retention_reaper.py`` never calls this;
    keep the helper as the contract for the message-branch reaper package.
    """
    if conversation_is_under_legal_hold(conv):
        return False
    if message_is_on_active_path(db, conv, msg.id):
        return False
    return branch_retention_days() is not None


def _dialect_name(db: Session) -> str:
    return db.get_bind().dialect.name


def _newest_leaf_id(db: Session, conv_id: int) -> Optional[int]:
    """Leaf with the greatest (created_at, id) among messages that have no child."""
    row = db.execute(
        text(
            """
            SELECT m.id
            FROM messages m
            WHERE m.conversation_id = :cid
              AND NOT EXISTS (
                  SELECT 1 FROM messages c
                  WHERE c.conversation_id = m.conversation_id
                    AND c.parent_id = m.id
              )
            ORDER BY m.created_at DESC, m.id DESC
            LIMIT 1
            """
        ),
        {"cid": conv_id},
    ).first()
    return int(row[0]) if row else None


def resolve_active_leaf_id(db: Session, conv: Conversation) -> Optional[int]:
    if conv.active_leaf_message_id is not None:
        return conv.active_leaf_message_id
    return _newest_leaf_id(db, conv.id)


def resolve_active_path(db: Session, conv: Conversation) -> list[Message]:
    """Active path from root → leaf.

    PostgreSQL uses a recursive CTE (one round-trip). SQLite (unit tests)
    walks parent_id in Python after a single conversation-scoped fetch —
    still O(n) messages, not N+1 per hop.
    """
    leaf_id = resolve_active_leaf_id(db, conv)
    if leaf_id is None:
        return []

    if _dialect_name(db) == "postgresql":
        rows = db.execute(
            text(
                """
                WITH RECURSIVE chain AS (
                    SELECT id, parent_id, 1 AS depth
                    FROM messages
                    WHERE id = :leaf AND conversation_id = :cid
                    UNION ALL
                    SELECT m.id, m.parent_id, chain.depth + 1
                    FROM messages m
                    JOIN chain ON m.id = chain.parent_id
                    WHERE m.conversation_id = :cid
                )
                SELECT id FROM chain ORDER BY depth DESC
                """
            ),
            {"leaf": leaf_id, "cid": conv.id},
        ).fetchall()
        ids = [int(r[0]) for r in rows]
        if not ids:
            return []
        by_id = {
            m.id: m
            for m in db.query(Message)
            .filter(Message.conversation_id == conv.id, Message.id.in_(ids))
            .all()
        }
        return [by_id[i] for i in ids if i in by_id]

    # SQLite / other: one fetch, reverse walk.
    all_msgs = (
        db.query(Message)
        .filter(Message.conversation_id == conv.id)
        .all()
    )
    by_id = {m.id: m for m in all_msgs}
    chain: list[Message] = []
    cur = by_id.get(leaf_id)
    seen: set[int] = set()
    while cur is not None and cur.id not in seen:
        seen.add(cur.id)
        chain.append(cur)
        cur = by_id.get(cur.parent_id) if cur.parent_id is not None else None
    chain.reverse()
    return chain


def list_messages_for_read(
    db: Session, conv: Conversation, *, tree: bool = False
) -> list[Message]:
    """Default = active path (old-client shape). ``tree=True`` = full tree."""
    if tree:
        return (
            db.query(Message)
            .filter(Message.conversation_id == conv.id)
            .order_by(Message.created_at.asc(), Message.id.asc())
            .all()
        )
    return resolve_active_path(db, conv)


def list_siblings(db: Session, conv: Conversation, message: Message) -> list[Message]:
    """Sibling set under the same parent (includes ``message`` itself)."""
    q = db.query(Message).filter(Message.conversation_id == conv.id)
    if message.parent_id is None:
        q = q.filter(Message.parent_id.is_(None))
    else:
        q = q.filter(Message.parent_id == message.parent_id)
    return q.order_by(Message.created_at.asc(), Message.id.asc()).all()


def _require_edit_allowed(db: Session, conv: Conversation, user: User) -> None:
    """Edit gates: legal hold + classification posture (C3 / W2-3).

    Access (owner/admin) is enforced by the caller via ``get_conversation``.
    This helper refuses legal-hold conversations and fails closed on a
    corrupt/unknown classification_level. Classification level itself does
    not add a distinct edit rule beyond that access check.
    """
    if conversation_is_under_legal_hold(conv):
        raise HTTPException(
            status_code=403,
            detail="此對話處於法律保全，不可編輯或重生訊息",
        )
    # Touch classification level so a corrupt/unknown value fails closed the
    # same way classify paths do (via is_controlled / from_storage).
    try:
        ClassificationLevel.from_storage(conv.classification_level)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=403,
            detail="對話密等狀態異常，拒絕編輯",
        ) from exc
    _ = user  # ownership already enforced by get_conversation


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
) -> Message:
    """Create a message under ``parent_id``, or under the active leaf.

    ``parent_id=None`` means "extend the active branch": the new row hangs
    under ``conversations.active_leaf_message_id`` so linear chat stays a
    single chain. **That default is correct for exactly one kind of caller** —
    a brand-new user turn continuing the branch the user is currently looking
    at, which is precisely what the active leaf points at.

    It is WRONG for every caller that means "sibling". The active leaf is
    server-side state that other actions move: after a failed regenerate it
    still points at the PREVIOUS assistant, so a recovery append lands as that
    assistant's *child* instead of its sibling — one level too deep — and the
    next fork then branches from the wrong level. Nothing here can tell the two
    intents apart, so a caller meaning "sibling" must either pass ``parent_id``
    explicitly or go through ``fork_assistant_message`` / ``edit_user_message``,
    which derive the parent from a named source row.

    Either way the new row becomes ``active_leaf_message_id``.
    """
    _check_metadata_size(metadata)
    conv = get_conversation(db, conv_id, user)
    if parent_id is None:
        parent_id = resolve_active_leaf_id(db, conv)
    elif parent_id is not None:
        parent = (
            db.query(Message)
            .filter(Message.id == parent_id, Message.conversation_id == conv.id)
            .first()
        )
        if parent is None:
            raise HTTPException(status_code=400, detail="parent_id 不屬於此對話")
    msg = Message(
        conversation_id=conv.id,
        parent_id=parent_id,
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
    conv.active_leaf_message_id = msg.id
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
    """Grow a sibling user node under the same parent; preserve the old subtree.

    W2-3 / C3: editing must NOT truncate/delete trailing messages. The new
    sibling becomes the active leaf; the UI re-sends the chat turn to grow a
    fresh assistant child under it.
    """
    conv = get_conversation(db, conv_id, user)
    _require_edit_allowed(db, conv, user)
    msg = (
        db.query(Message)
        .filter(Message.id == message_id, Message.conversation_id == conv.id)
        .first()
    )
    if msg is None:
        raise HTTPException(status_code=404, detail="訊息不存在")
    if msg.role != "user":
        raise HTTPException(status_code=400, detail="僅使用者訊息可編輯")

    sibling = Message(
        conversation_id=conv.id,
        parent_id=msg.parent_id,
        role="user",
        content=content,
        classification_level=msg.classification_level,
        classification_source=msg.classification_source,
    )
    db.add(sibling)
    db.flush()
    conv.active_leaf_message_id = sibling.id
    conv.updated_at = datetime.now(timezone.utc)
    log_audit_event(
        db,
        action="edit_user_message",
        resource_type="message",
        actor=user,
        resource_id=sibling.id,
        detail=(
            f"User {user.username} edited message {message_id} in conversation "
            f"{conv_id} → sibling {sibling.id} (old subtree preserved)"
        ),
        metadata={
            "conversation_id": conv_id,
            "source_message_id": message_id,
            "sibling_message_id": sibling.id,
            "classification_level": conv.classification_level,
        },
        commit=False,
    )
    db.commit()
    db.refresh(sibling)
    return sibling


def fork_assistant_message(
    db: Session,
    conv_id: int,
    message_id: int,
    user: User,
    *,
    content: str,
    trace_id: Optional[str] = None,
    latency_ms: Optional[int] = None,
    model_name: Optional[str] = None,
    agent_name: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> Message:
    """Regenerate: create a sibling assistant under the same parent (C3 §b)."""
    _check_metadata_size(metadata)
    conv = get_conversation(db, conv_id, user)
    _require_edit_allowed(db, conv, user)
    source = (
        db.query(Message)
        .filter(Message.id == message_id, Message.conversation_id == conv.id)
        .first()
    )
    if source is None:
        raise HTTPException(status_code=404, detail="訊息不存在")
    if source.role != "assistant":
        raise HTTPException(status_code=400, detail="僅助理訊息可重生")

    sibling = Message(
        conversation_id=conv.id,
        parent_id=source.parent_id,
        role="assistant",
        content=content,
        trace_id=trace_id,
        latency_ms=latency_ms,
        model_name=model_name,
        agent_name=agent_name,
        metadata_=metadata,
        classification_level=source.classification_level,
        classification_source=source.classification_source,
    )
    db.add(sibling)
    db.flush()
    conv.active_leaf_message_id = sibling.id
    conv.updated_at = datetime.now(timezone.utc)
    log_audit_event(
        db,
        action="fork_assistant_message",
        resource_type="message",
        actor=user,
        resource_id=sibling.id,
        detail=(
            f"User {user.username} regenerated assistant message {message_id} "
            f"in conversation {conv_id} → sibling {sibling.id}"
        ),
        metadata={
            "conversation_id": conv_id,
            "source_message_id": message_id,
            "sibling_message_id": sibling.id,
        },
        commit=False,
    )
    db.commit()
    db.refresh(sibling)
    return sibling


def _deepest_leaf_under(db: Session, conv_id: int, root_id: int) -> int:
    """Deepest leaf in the subtree rooted at ``root_id`` (inclusive).

    When ``root_id`` already is a leaf, returns it. With multiple descendant
    leaves, picks the newest by ``(created_at, id)``.
    """
    if _dialect_name(db) == "postgresql":
        row = db.execute(
            text(
                """
                WITH RECURSIVE subtree AS (
                    SELECT id, parent_id, created_at
                    FROM messages
                    WHERE id = :root AND conversation_id = :cid
                    UNION ALL
                    SELECT m.id, m.parent_id, m.created_at
                    FROM messages m
                    JOIN subtree s ON m.parent_id = s.id
                    WHERE m.conversation_id = :cid
                )
                SELECT s.id
                FROM subtree s
                WHERE NOT EXISTS (
                    SELECT 1 FROM messages c
                    WHERE c.conversation_id = :cid AND c.parent_id = s.id
                )
                ORDER BY s.created_at DESC, s.id DESC
                LIMIT 1
                """
            ),
            {"root": root_id, "cid": conv_id},
        ).first()
        return int(row[0]) if row else root_id

    all_msgs = (
        db.query(Message).filter(Message.conversation_id == conv_id).all()
    )
    children: dict[int, list[Message]] = {}
    by_id = {m.id: m for m in all_msgs}
    if root_id not in by_id:
        return root_id
    for m in all_msgs:
        if m.parent_id is not None:
            children.setdefault(m.parent_id, []).append(m)
    subtree: set[int] = set()
    stack = [root_id]
    while stack:
        cur = stack.pop()
        if cur in subtree:
            continue
        subtree.add(cur)
        for child in children.get(cur, []):
            stack.append(child.id)
    newest: Optional[Message] = None
    for mid in subtree:
        if children.get(mid):
            continue
        m = by_id[mid]
        if newest is None or (m.created_at, m.id) > (newest.created_at, newest.id):
            newest = m
    return newest.id if newest else root_id


def set_active_leaf(
    db: Session, conv_id: int, message_id: int, user: User
) -> Conversation:
    """Point the conversation's active path at a branch (version switch).

    ``message_id`` may be any node on the desired branch; the stored pointer
    is that branch's deepest leaf so continuations survive a reload.
    """
    conv = get_conversation(db, conv_id, user)
    _require_edit_allowed(db, conv, user)
    msg = (
        db.query(Message)
        .filter(Message.id == message_id, Message.conversation_id == conv.id)
        .first()
    )
    if msg is None:
        raise HTTPException(status_code=404, detail="訊息不存在")
    conv.active_leaf_message_id = _deepest_leaf_under(db, conv.id, msg.id)
    conv.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(conv)
    return conv


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
    """In-place update of an existing message (stream checkpoint / resume).

    Regenerate must use :func:`fork_assistant_message` so the old revision
    stays in the tree. This path stays non-forking for mid-stream upserts.
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

    Slice 3b: routes through the five-level one-way core
    (``apply_classification`` reason=``manual_admin``) which writes the
    ClassificationEvent, sets ``classification_level=機密`` and mirrors the
    legacy boolean (``classified=True``). ``classified_by`` is an
    admin-attribution field the event model doesn't carry, so it's stamped
    here alongside the existing AuditLog trail.
    """
    conv = get_conversation(db, conv_id, user)
    if conv.classified:
        raise HTTPException(status_code=409, detail="此對話已標示為機密")
    from app.modules.policy import apply_classification
    apply_classification(
        db,
        resource_type="conversation",
        resource_id=str(conv_id),
        new_level="機密",
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


def log_controlled_access(
    db: Session, conv_id: int, user: User, *, level_label: str, event: str = "讀取"
) -> None:
    """對受控對話(密等 > 無機密)的讀取落一列稽核。

    W1-1:原名 `log_classified_access`,而且**只在 legacy `classified` 為 True
    時被呼叫** —— 也就是只有 `>= 機密` 才記帳,營業秘密的讀取一列都沒有。

    `action` 字串刻意**不改**(仍是 `access_classified_conversation`):改了會把
    歷史稽核軌跡切成兩個 action 名,「誰讀過受控對話」這種查詢會漏掉舊列。實際
    密等寫進 `detail`,讓事後能分辨營業秘密與絕對機密 —— 沒有這個,稽核只剩
    「有人看過某個受控東西」。

    Field names match the AuditLog model exactly: actor_user_id /
    actor_username / detail (singular). The previous spelling
    ``user_id`` / ``details`` slipped through because no code path
    actually triggered classified-access logging until the
    conversations.classified column started being persisted by
    ``_latch_agent_classification`` — at which point GET /api/conversations/:id
    blew up with TypeError on construction.
    """
    db.add(AuditLog(
        actor_user_id=user.id,
        actor_username=user.username,
        action="access_classified_conversation",
        resource_type="conversation",
        resource_id=str(conv_id),
        status="success",
        detail=(
            f"使用者 {user.username} {event}密等「{level_label}」的對話 {conv_id}"
        ),
    ))
    db.commit()


# ── Share links ───────────────────────────────────────────────────────────────

def controlled_level_label(conv: Conversation) -> str:
    """人可讀的密等標籤;未知值回「未知(視同受控)」。

    稽核與使用者面文案都要顯示實際密等,而不是「classified / not classified」。
    """
    try:
        return ClassificationLevel.from_storage(conv.classification_level).to_storage()
    except (TypeError, ValueError):
        return "未知(視同受控)"


def is_controlled(conv: Conversation) -> bool:
    """密等是否高於「無機密」→ 一切外流面都要當受控處理。

    **這是五個外流面(複製 / 匯出 / 分享 / 列印 / 稽核)的單一判定來源。** W1-1
    之前每個面各自寫,而其中兩個寫的是 legacy `classified` boolean —— 那個
    boolean 的鏡射規則是 `classified = level >= 機密`(`api/conversations.py:125`
    自己寫明),所以**營業秘密的 `classified` 是 False**,於是營業秘密在那兩個
    面上等同無機密。

    未知或損壞的儲存值 **fail-closed 視同受控**。門檻是「> 無機密」而不是
    「>= 機密」—— 後者正是第一輪犯的錯,計畫的風險欄把它記為「淨退化陷阱」。
    """
    try:
        return (
            ClassificationLevel.from_storage(conv.classification_level)
            is not ClassificationLevel.UNCLASSIFIED
        )
    except (TypeError, ValueError):
        return True


def is_publicly_shareable(conv: Conversation) -> bool:
    """Return True only for a valid, explicitly UNCLASSIFIED conversation.

    The legacy ``classified`` boolean is only a compatibility read model and
    remains False for 營業秘密, so it is not an authorization input. Unknown or
    malformed stored values fail closed.

    W1-1:改為委派給 `is_controlled()`,讓分享面與其餘四個外流面共用同一個定義
    (行為不變 —— 兩者本來就是同一判定的正反面,但先前是兩份會各自漂移的實作)。
    """
    return not is_controlled(conv)


def _stored_datetime_as_utc(value: datetime) -> datetime:
    """Normalize DB datetimes to UTC.

    ``ConversationShare.expires_at`` is a legacy timezone-naive column. SQLite
    and PostgreSQL can therefore return a naive value even when the API wrote
    an aware UTC datetime. Stored values have always represented UTC.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _bounded_share_expiration(expires_at: Optional[datetime]) -> datetime:
    now = datetime.now(timezone.utc)
    maximum = now + timedelta(hours=settings.PUBLIC_SHARE_MAX_TTL_HOURS)
    if expires_at is None:
        return maximum
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise HTTPException(
            status_code=400,
            detail="分享連結到期時間必須包含時區",
        )
    normalized = expires_at.astimezone(timezone.utc)
    if normalized <= now:
        raise HTTPException(status_code=400, detail="分享連結到期時間必須在未來")
    if normalized > maximum:
        raise HTTPException(
            status_code=400,
            detail=(
                "分享連結期限不可超過 "
                f"{settings.PUBLIC_SHARE_MAX_TTL_HOURS} 小時"
            ),
        )
    return normalized

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
    # W3-7c:**旗標面**的 gate。這與下面的分類面 gate 是**兩個不同缺陷**
    # (CLAUDE.md §5.1 明確分開記),別混為一談。
    #
    # 先前這裡完全沒讀 `ENABLE_PUBLIC_SHARE`,而讀取端有擋
    # (`api/public_share.py:50-53` 回 404)。card 部署的姿態是 false,結果:
    # 使用者按下「建立分享連結」→ 成功、拿到 URL、UI 顯示成功 → 傳給同事 →
    # 同事**一定**看到 404。平台給了一個保證壞掉的東西,而且是使用者主動要
    # 拿去給別人的東西。
    #
    # 回 403 而不是 404:讀取端用 404 是因為那條路徑未經認證,404 讓 token 的
    # 存在無法被探測。這裡呼叫者已認證、操作的是自己的對話,沒有可探測的東西;
    # 而 `GET /api/capabilities` 本來就會把 `enable_public_share` 告訴已認證的
    # 使用者(W1-3 刻意放進去讓前端隱藏這顆鈕)。所以給明確訊息,讓使用者知道
    # 「不是你做錯,是這個部署沒開」。
    #
    # 刻意的不對稱:**建立**擋、**列出**不擋 —— 旗標關掉之前建的連結還是要
    # 看得到,否則使用者無法撤銷已經發出去的東西。
    if not settings.ENABLE_PUBLIC_SHARE:
        raise HTTPException(
            status_code=403,
            detail="本部署未啟用公開分享功能,無法建立分享連結",
        )
    if not is_publicly_shareable(conv):
        raise HTTPException(
            status_code=403,
            detail="只有無機密對話可建立分享連結",
        )
    bounded_expires_at = _bounded_share_expiration(expires_at)
    share = ConversationShare(
        conversation_id=conv.id,
        token=secrets.token_urlsafe(32),
        mode=mode,
        allow_fork=allow_fork,
        expires_at=bounded_expires_at,
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
    # Re-check the conversation's current five-level classification on every
    # anonymous read. Upgrading a conversation immediately invalidates all old
    # tokens without disclosing whether the token itself was ever valid.
    if not is_publicly_shareable(share.conversation):
        raise HTTPException(status_code=404, detail="找不到此分享連結")
    if (
        share.expires_at
        and _stored_datetime_as_utc(share.expires_at) < datetime.now(timezone.utc)
    ):
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
    get_conversation(db, share.conversation_id, user)  # ownership check
    db.delete(share)
    db.commit()


# ── Internal helpers ──────────────────────────────────────────────────────────

def _check_access(conv: Conversation, user: User) -> None:
    if is_admin_tier(user):
        return
    if conv.user_id != user.id:
        raise HTTPException(status_code=403, detail="無權存取此對話")
