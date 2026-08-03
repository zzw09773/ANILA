"""Conversation persistence and named-share service."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import or_, update as sa_update
from sqlalchemy.orm import Session

from app.config import settings
from app.models.attachment import Attachment
from app.models.audit_log import AuditLog
from app.models.conversation import Conversation, ConversationShare, ConversationUserMeta
from app.models.department import Department
from app.models.message import Message
from app.models.user import User
from app.services.audit_service import log_audit_event
from app.services.auth_service import is_admin_tier
from app.services import message_tree as mtree
from app.services import zh_normalize_service
from app.services.usage_service import _department_scope_ids


# Client-supplied message metadata is an opaque dict persisted verbatim
# (trace_id, latency, model/agent name, structured feedback). Cap its
# serialized size so an authenticated insider can't bloat a row; 64KB is far
# above any legitimate metadata payload.
_MAX_METADATA_BYTES = 64 * 1024

# start_turn 的 compare-and-swap 重試上限。每次重試都代表另一個 client 在
# 同一瞬間推進了同一個對話的 leaf；連續撞這麼多次已經不是「兩個分頁」，
# 停下來報 409 比無限重試安全。
_START_TURN_MAX_ATTEMPTS = 5


# ── 串流前預留助理訊息（reserve-then-stream） ─────────────────────────────────
#
# 助理訊息在串流「開始之前」就先落庫成一列空白的 reserved 列。理由是 active
# leaf：舊流程要等串流跑完才 append 助理訊息，所以整段串流期間 leaf 都還停在
# 使用者訊息上。使用者在串流中途按 Enter 再送一則，預設的 leaf 解析就把第二
# 則使用者訊息掛成第一則使用者訊息的子節點，等第一則助理回覆終於要落庫時，
# _enforce_explicit_parent_role 以 400 擋下——而訊息樹在報錯之前就已經錯了。
# 先預留這一列之後 leaf 落在助理訊息上，後續使用者訊息的預設解析自然正確，
# 不需要放寬任何既有的不變式。
#
# ⚠ 已知缺陷（2026-08-03，刻意不在這一輪修，記在這裡不要讓它變成口耳相傳）：
# **孤兒 reserved 列沒有回收機制。**
#   前端在關視窗／重整時會用 pagehide 把自己預留的每一列（含還在排隊、尚未
#   開始串流的那些）標成 interrupted。但那是 best-effort：瀏覽器當掉、斷電、
#   分頁被作業系統殺掉時那個請求送不出去，那一列就永遠停在 reserved。
#   後果，說清楚：
#     • 不是資料遺失 —— 使用者的問題已經落庫，UI 會誠實標示那一列沒寫完，
#       後續的對話照常繼續，空內容的列也不會進到送給模型的上下文。
#     • 但那個答案的位置永遠是空的，而且**永遠寫不進去**：它掛著一個沒有人
#       持有的 writer 權杖，帶錯權杖、不帶權杖、只改 metadata 一律 409。
#       未來任何想 in-place 寫它的功能（ANILALM finalize、Continue Response）
#       打到這一列都會 409。
#   要關掉它需要一個 reaper（或在 envelope 裡放時間戳、逾時後允許接管）。
#   整個 repo 目前沒有任何 reaper。
STREAM_META_KEY = "anila_stream"

STREAM_STATE_RESERVED = "reserved"        # 已預留，內容尚未產生
STREAM_STATE_STREAMING = "streaming"      # 串流中，內容是部分的
STREAM_STATE_COMPLETE = "complete"        # 正常結束，內容完整
STREAM_STATE_STOPPED = "stopped"          # 使用者按停止，內容是部分的
STREAM_STATE_FAILED = "failed"            # 串流出錯，內容是部分的
STREAM_STATE_INTERRUPTED = "interrupted"  # 前端消失（關分頁／重整／當掉）

# 終局狀態 = 不會再有人寫這一列。寫入者權杖在進入終局時一併清掉，
# 後續的 ANILALM finalize / Continue Response 等 in-place patch 不受影響。
STREAM_TERMINAL_STATES = frozenset(
    {
        STREAM_STATE_COMPLETE,
        STREAM_STATE_STOPPED,
        STREAM_STATE_FAILED,
        STREAM_STATE_INTERRUPTED,
    }
)

# 內容不完整、但不是「還在跑」的狀態。UI 必須據此標示，
# 絕不能把半截答案當成完整答案呈現。
STREAM_INCOMPLETE_STATES = frozenset(
    {STREAM_STATE_STOPPED, STREAM_STATE_FAILED, STREAM_STATE_INTERRUPTED}
)

_STREAM_ALL_STATES = frozenset(
    {STREAM_STATE_RESERVED, STREAM_STATE_STREAMING} | STREAM_TERMINAL_STATES
)


def stream_envelope(metadata: Optional[dict]) -> Optional[dict]:
    """Return the ``anila_stream`` envelope of a message's metadata, if any."""
    if not isinstance(metadata, dict):
        return None
    envelope = metadata.get(STREAM_META_KEY)
    return envelope if isinstance(envelope, dict) else None


def _active_stream_writer(metadata: Optional[dict]) -> Optional[str]:
    """Writer token of a row that is still reserved/streaming, else None.

    終局狀態一律回 None —— 那一列已經沒有寫入者，任何人都可以正常 patch。
    """
    envelope = stream_envelope(metadata)
    if envelope is None:
        return None
    if envelope.get("state") in STREAM_TERMINAL_STATES:
        return None
    writer = envelope.get("writer")
    return writer if isinstance(writer, str) and writer else None


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


def _resolve_agent_for_adopt(
    db: Session,
    user: User,
    *,
    agent_id: Optional[int] = None,
    agent_name: Optional[str] = None,
):
    """Resolve an approved Agent the caller may use. None when absent/denied.

    Mirrors chat-proxy discovery (approved + permission) so adopt cannot latch
    classification via an agent the user could not have compared against.
    """
    from app.models.agent import Agent
    from app.services.api_key_service import check_agent_permission

    agent = None
    if agent_id is not None:
        agent = (
            db.query(Agent)
            .filter(Agent.id == agent_id, Agent.approval_status == "approved")
            .first()
        )
    else:
        name = (agent_name or "").strip()
        if name:
            agent = (
                db.query(Agent)
                .filter(Agent.name == name, Agent.approval_status == "approved")
                .first()
            )
    if agent is None:
        return None
    if not is_admin_tier(user) and not check_agent_permission(
        db, user=user, api_key_id=None, agent_id=agent.id,
    ):
        return None
    return agent


def _latch_agent_policy_on_conversation(db: Session, conv_id: int, agent) -> None:
    """Mirror chat-proxy agent-policy latch (reason=agent_policy).

    Uses ``effective_agent_policy_level`` (same rule as proxy). No-op when
    the effective level is 無機密.
    """
    from app.api.agents._common import effective_agent_policy_level
    from app.modules.policy import apply_classification
    from app.schemas.contracts.classification import ClassificationLevel

    level = effective_agent_policy_level(agent)
    if level <= ClassificationLevel.UNCLASSIFIED:
        return
    apply_classification(
        db,
        resource_type="conversation",
        resource_id=str(conv_id),
        new_level=level.to_storage(),
        actor_type="service",
        actor_id="agent-policy",
        reason="agent_policy",
        source="agent_policy",
    )


def adopt_compare_answer(
    db: Session,
    user: User,
    *,
    title: str,
    user_content: str,
    assistant_content: str,
    agent_id: Optional[int] = None,
    agent_name: Optional[str] = None,
    origin: Optional[str] = "anila-ui",
    assistant_metadata: Optional[dict] = None,
    assistant_trace_id: Optional[str] = None,
    assistant_latency_ms: Optional[int] = None,
    assistant_agent_name: Optional[str] = None,
) -> Conversation:
    """Promote a compare-mode answer into a real conversation + message tree.

    Creates the conversation, appends user then assistant (parent_id=user via
    active leaf), then latches classification from the resolved agent's policy
    — the same path ordinary chat takes via the proxy. Does not re-call the
    model.
    """
    _check_metadata_size(assistant_metadata)
    agent = _resolve_agent_for_adopt(
        db, user, agent_id=agent_id, agent_name=agent_name,
    )
    # Only persist a real FK — never write an unresolved agent_id (would 500).
    resolved_agent_id = agent.id if agent is not None else None
    conv = create_conversation(
        db,
        user.id,
        title=title or "採用比較結果",
        agent_id=resolved_agent_id,
        origin=origin,
        collection_id=None,
    )
    # Same chaining ordinary chat uses: user lands as active leaf, then
    # assistant threads onto it (no explicit parent_id needed).
    append_message(
        db,
        conv.id,
        user,
        role="user",
        content=user_content,
        set_active=True,
    )
    append_message(
        db,
        conv.id,
        user,
        role="assistant",
        content=assistant_content,
        trace_id=assistant_trace_id,
        latency_ms=assistant_latency_ms,
        agent_name=assistant_agent_name,
        metadata=assistant_metadata,
        set_active=True,
    )
    if agent is not None:
        _latch_agent_policy_on_conversation(db, conv.id, agent)
    db.refresh(conv)
    return conv


def get_conversation(
    db: Session,
    conv_id: int,
    user: User,
    *,
    for_write: bool = True,
) -> Conversation:
    """Load a conversation with access control.

    ``for_write=True`` (default): owner or admin — mutations, share
    management. ``for_write=False``: also allows an active named share
    targeting the caller (P4.3 read path).
    """
    conv = db.query(Conversation).filter(Conversation.id == conv_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="找不到此對話")
    if for_write:
        _check_access(conv, user)
    else:
        _check_read_access(db, conv, user)
    return conv


def list_conversations(
    db: Session,
    user: User,
    origin: Optional[str] = None,
    exclude_origin: Optional[str] = None,
    collection_id: Optional[int] = None,
) -> list[Conversation]:
    """List the caller's conversations, optionally filtered.

    Includes conversations the caller owns **and** those shared with them
    via an active named share (P4.3). Origin/collection filters apply to
    both sets.

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
    shared_ids = _shared_conversation_ids_for_user(db, user)
    ownership = Conversation.user_id == user.id
    if shared_ids:
        q = db.query(Conversation).filter(
            or_(ownership, Conversation.id.in_(shared_ids))
        )
    else:
        q = db.query(Conversation).filter(ownership)
    if origin is not None:
        q = q.filter(Conversation.origin == origin)
    elif exclude_origin is not None:
        q = q.filter(
            (Conversation.origin.is_(None)) | (Conversation.origin != exclude_origin)
        )
    if collection_id is not None:
        q = q.filter(Conversation.collection_id == collection_id)
    return q.order_by(Conversation.updated_at.desc()).all()


# System-derived tag — never persisted in conversation_user_meta.user_tags.
CLASSIFIED_TAG = "classified"
_MAX_USER_TAGS = 32
_MAX_TAG_LEN = 40
_MAX_FOLDER_LEN = 64


def _sanitize_user_tags(raw: list | None) -> list[str]:
    """Drop the derived classified tag and empty/oversized entries."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="tags 必須是字串陣列")
    if len(raw) > _MAX_USER_TAGS:
        raise HTTPException(
            status_code=400,
            detail=f"標籤最多 {_MAX_USER_TAGS} 個",
        )
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise HTTPException(status_code=400, detail="tags 必須是字串陣列")
        tag = item.strip()
        if not tag or tag == CLASSIFIED_TAG:
            continue
        if len(tag) > _MAX_TAG_LEN:
            raise HTTPException(
                status_code=400,
                detail=f"單一標籤最長 {_MAX_TAG_LEN} 字元",
            )
        if tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    return out


def _sanitize_folder(folder: str | None) -> str:
    if folder is None:
        return "all"
    if not isinstance(folder, str):
        raise HTTPException(status_code=400, detail="folder 必須是字串")
    cleaned = folder.strip() or "all"
    if len(cleaned) > _MAX_FOLDER_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"folder 最長 {_MAX_FOLDER_LEN} 字元",
        )
    # Built-in filter ids are not storage folders.
    if cleaned in ("starred",):
        raise HTTPException(status_code=400, detail="不可將對話歸入內建篩選匣")
    return cleaned


def get_user_meta(
    db: Session, user_id: int, conversation_id: int,
) -> ConversationUserMeta | None:
    return (
        db.query(ConversationUserMeta)
        .filter(
            ConversationUserMeta.user_id == user_id,
            ConversationUserMeta.conversation_id == conversation_id,
        )
        .first()
    )


def load_user_metas(
    db: Session, user_id: int, conversation_ids: list[int],
) -> dict[int, ConversationUserMeta]:
    """Batch-load meta for the list endpoint — one query, not N."""
    if not conversation_ids:
        return {}
    rows = (
        db.query(ConversationUserMeta)
        .filter(
            ConversationUserMeta.user_id == user_id,
            ConversationUserMeta.conversation_id.in_(conversation_ids),
        )
        .all()
    )
    return {r.conversation_id: r for r in rows}


def meta_view(
    conv: Conversation, meta: ConversationUserMeta | None,
) -> dict:
    """Public view fields for the current user (classified tag derived)."""
    user_tags = [
        t for t in list((meta.user_tags if meta else None) or [])
        if isinstance(t, str) and t and t != CLASSIFIED_TAG
    ]
    tags = list(user_tags)
    if conv.classified and CLASSIFIED_TAG not in tags:
        tags.append(CLASSIFIED_TAG)
    return {
        "starred": bool(meta.starred) if meta else False,
        "folder": (meta.folder if meta and meta.folder else "all"),
        "tags": tags,
    }


def upsert_user_meta(
    db: Session,
    user: User,
    conv: Conversation,
    *,
    starred: bool | None = None,
    folder: str | None = None,
    tags: list | None = None,
) -> ConversationUserMeta:
    """Create or patch the caller's meta row. Does not touch title."""
    row = get_user_meta(db, user.id, conv.id)
    if row is None:
        row = ConversationUserMeta(
            user_id=user.id,
            conversation_id=conv.id,
            starred=False,
            folder="all",
            user_tags=[],
        )
        db.add(row)
    if starred is not None:
        row.starred = bool(starred)
    if folder is not None:
        row.folder = _sanitize_folder(folder)
    if tags is not None:
        row.user_tags = _sanitize_user_tags(tags)
    row.updated_at = datetime.now(timezone.utc)
    return row


def update_title(db: Session, conv_id: int, title: str, user: User) -> Conversation:
    conv = get_conversation(db, conv_id, user)
    conv.title = title
    conv.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(conv)
    return conv


def update_conversation(
    db: Session,
    conv_id: int,
    user: User,
    *,
    title: str | None = None,
    starred: bool | None = None,
    folder: str | None = None,
    tags: list | None = None,
) -> Conversation:
    """Patch title (owner/admin) and/or the caller's personal meta.

    Meta updates only need read access so a named-share recipient can
    star / file / tag their own view without write rights on the thread.
    """
    meta_touch = starred is not None or folder is not None or tags is not None
    if title is None and not meta_touch:
        raise HTTPException(status_code=400, detail="沒有可更新的欄位")
    if title is not None:
        conv = get_conversation(db, conv_id, user, for_write=True)
        cleaned = title.strip()
        if not cleaned:
            raise HTTPException(status_code=400, detail="標題不可為空")
        conv.title = cleaned
        conv.updated_at = datetime.now(timezone.utc)
    else:
        conv = get_conversation(db, conv_id, user, for_write=False)
    if meta_touch:
        upsert_user_meta(
            db, user, conv, starred=starred, folder=folder, tags=tags,
        )
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
    (
        db.query(ConversationUserMeta)
        .filter(ConversationUserMeta.conversation_id == conv.id)
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
    # §6-3：assistant 落庫前靜默 s2twp＋域內用語；user 原文不動（fail-open）
    content, zh_changed = zh_normalize_service.prepare_message_content(role, content)
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
    zh_normalize_service.log_if_changed(msg.id, zh_changed)
    # set_active=false only declines to MOVE an existing pointer; never leave NULL.
    if set_active or conv.active_leaf_message_id is None:
        conv.active_leaf_message_id = msg.id
    conv.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(msg)
    return msg


def _reserved_metadata(writer: str) -> dict:
    return {
        STREAM_META_KEY: {
            "state": STREAM_STATE_RESERVED,
            "writer": writer,
        }
    }


def reserve_assistant_reply(
    db: Session,
    conv_id: int,
    parent_message_id: int,
    user: User,
    *,
    writer: str,
    model_name: Optional[str] = None,
    agent_name: Optional[str] = None,
) -> Message:
    """Reserve an empty assistant row under a user message, before streaming.

    這條路徑刻意「不是」分支：parent 必須是一則還沒有任何子訊息的 user
    訊息，所以它只可能是線性接續。因此不需要 _require_branchable
    （ANILALM 不支援分支的規則不受影響），也不會有同層角色衝突。

    回傳的列 content='' 且 metadata.anila_stream.state='reserved'，
    active leaf 前進到這一列。
    """
    if not isinstance(writer, str) or not writer:
        raise HTTPException(status_code=400, detail="缺少串流寫入者權杖")
    conv = get_conversation(db, conv_id, user)
    # 與 append_message 相同的序列化邊界：預留和 append 會競爭同一個 leaf。
    conv = _lock_conversation(db, conv.id)
    parent = (
        db.query(Message)
        .filter(Message.id == parent_message_id, Message.conversation_id == conv.id)
        .first()
    )
    if parent is None:
        raise HTTPException(status_code=404, detail="父訊息不存在")
    if parent.role != "user":
        raise HTTPException(
            status_code=400,
            detail="只能在使用者訊息底下預留助理回覆",
        )
    if _sibling_count(db, conv.id, parent.id) > 0:
        # 已經有子訊息 = 這是分支，不是線性接續。走 /branch 才對。
        raise HTTPException(
            status_code=409,
            detail="這則使用者訊息已經有回覆，無法重複預留",
        )
    metadata = _reserved_metadata(writer)
    _check_metadata_size(metadata)
    msg = Message(
        conversation_id=conv.id,
        parent_id=parent.id,
        role="assistant",
        content="",
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


def start_turn(
    db: Session,
    conv_id: int,
    user: User,
    *,
    content: str,
    writer: str,
    model_name: Optional[str] = None,
    agent_name: Optional[str] = None,
) -> tuple[Message, Message]:
    """使用者訊息落庫 ＋ 助理列預留，在同一個交易裡完成。

    為什麼要合成一次：分成兩次 HTTP 往返時，兩者之間那段 RTT 就是一個窗口。
    兩個分頁同一瞬間按 Enter 的話：A 先 append 了 U_A（leaf=U_A），B 的
    append 走預設 leaf 解析掛到 U_A 底下（leaf=U_B），A 這時才來預留 U_A 的
    回覆——U_A 已經有子訊息了，於是 409。結果是 U_A 這則使用者訊息永遠拿不
    到答案，而且它明明已經在資料庫裡。

    合成一次之後，兩個分頁各自的「append + reserve」都在同一個
    ``_lock_conversation`` 交易內完成，後到的那個看到的 leaf 已經是前一個
    預留好的助理列，樹維持線性。

    ⚠ 這裡**不依賴** ``FOR UPDATE``。``_lock_conversation`` 在 PostgreSQL 上
    會先序列化，但它在 SQLite 上是 no-op，實測（2026-08-03，真 uvicorn＋真
    socket、兩個併發 client、5 回合）就會出現另一種壞法：兩邊都讀到同一個
    leaf，樹在根部長出兩個分岔。所以 leaf 的推進改用 compare-and-swap ——
    ``UPDATE ... WHERE active_leaf_message_id = 我讀到的那個``。0 rows 代表
    有人在這中間動過 leaf：整個交易回滾（訊息也一併回滾，不留半截），
    重讀 leaf 再來一次。這個機制在任何引擎上語意都一樣，而且測得到。

    回傳 ``(user_message, reserved_assistant_message)``；active leaf 停在
    預留的助理列上。
    """
    if not isinstance(writer, str) or not writer:
        raise HTTPException(status_code=400, detail="缺少串流寫入者權杖")
    conv = get_conversation(db, conv_id, user)
    conv_pk = conv.id
    metadata = _reserved_metadata(writer)
    _check_metadata_size(metadata)
    # §6-3：user 原文不動（fail-open），與 append_message 同一條正規化邊界。
    user_content, zh_changed = zh_normalize_service.prepare_message_content(
        "user", content,
    )

    for _attempt in range(_START_TURN_MAX_ATTEMPTS):
        # PostgreSQL 上這一行先把競爭者擋在門外；SQLite 上它什麼也不做，
        # 底下的 CAS 才是真正保證正確性的那一步。
        conv = _lock_conversation(db, conv_pk)
        expected_leaf = conv.active_leaf_message_id
        _enforce_sibling_cap(db, conv_pk, expected_leaf)

        user_msg = Message(
            conversation_id=conv_pk,
            parent_id=expected_leaf,
            role="user",
            content=user_content,
        )
        db.add(user_msg)
        db.flush()
        assistant_msg = Message(
            conversation_id=conv_pk,
            parent_id=user_msg.id,
            role="assistant",
            content="",
            model_name=model_name,
            agent_name=agent_name,
            metadata_=metadata,
        )
        db.add(assistant_msg)
        db.flush()

        stmt = (
            sa_update(Conversation)
            .where(Conversation.id == conv_pk)
            .values(
                active_leaf_message_id=assistant_msg.id,
                updated_at=datetime.now(timezone.utc),
            )
            .execution_options(synchronize_session=False)
        )
        stmt = stmt.where(
            Conversation.active_leaf_message_id.is_(None)
            if expected_leaf is None
            else Conversation.active_leaf_message_id == expected_leaf
        )
        if db.execute(stmt).rowcount == 1:
            db.commit()
            db.refresh(user_msg)
            db.refresh(assistant_msg)
            zh_normalize_service.log_if_changed(user_msg.id, zh_changed)
            return user_msg, assistant_msg

        # 有人在這中間推進了 leaf。整個交易丟掉（兩列都不會留下），重來。
        db.rollback()

    raise HTTPException(
        status_code=409,
        detail="這個對話同時有太多訊息在送出，請稍後再試一次",
    )


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
    # §6-3：與 append_message 同一落庫邊界（regenerate / edit-re-ask）
    content, zh_changed = zh_normalize_service.prepare_message_content(role, content)
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
    zh_normalize_service.log_if_changed(msg.id, zh_changed)
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
    stream_writer: Optional[str] = None,
) -> Message:
    """In-place patch of an existing message (metadata / ANILALM finalize).

    OW-1: ANILA regenerate no longer uses this path — it creates an assistant
    sibling via ``branch_message``. This endpoint remains for non-forking
    patches (ANILALM finalize, metadata updates) and for streaming content
    into a row reserved by ``reserve_assistant_reply``.

    預留列的所有權：只要那一列還掛著未終局的 writer 權杖，就只有持有相同
    權杖的呼叫端能寫它。兩個分頁各自預留自己的列，所以正常情況不會撞；
    這道閘門擋的是重播與寫錯列（409，不是靜默覆蓋）。終局後權杖清空，
    Continue Response / ANILALM finalize 等既有 patch 一律不受影響。
    """
    conv = get_conversation(db, conv_id, user)
    msg = (
        db.query(Message)
        .filter(Message.id == message_id, Message.conversation_id == conv.id)
        .first()
    )
    if msg is None:
        raise HTTPException(status_code=404, detail="訊息不存在")
    owner_token = _active_stream_writer(msg.metadata_)
    if owner_token is not None and stream_writer != owner_token:
        raise HTTPException(
            status_code=409,
            detail="這則回覆正由其他來源產生中，無法覆寫",
        )
    zh_changed = 0
    if content is not None:
        # §6-3：依既有訊息角色正規化（ANILALM finalize 等 in-place 寫入）
        content, zh_changed = zh_normalize_service.prepare_message_content(
            msg.role, content,
        )
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
        msg.metadata_ = _normalize_stream_envelope(metadata)
    conv.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(msg)
    zh_normalize_service.log_if_changed(msg.id, zh_changed)
    return msg


def _normalize_stream_envelope(metadata: dict) -> dict:
    """Validate an incoming ``anila_stream`` envelope and strip spent tokens.

    未知 state 直接擋（400）——沉默接受一個沒人看得懂的狀態，就是把
    「半截答案」偽裝成完整答案的那條路。進入終局時清掉 writer 權杖，
    這一列從此對一般 patch 開放。

    未終局的 state 也擋（400）。未終局的 envelope 只能由
    ``reserve_assistant_reply`` / ``start_turn`` 建立：如果 PUT 也能寫，
    任何人都能替自己的任何一則訊息裝上一個只有自己知道的 writer 權杖，
    之後那一列的每一次 in-place patch（ANILALM finalize、Continue
    Response）都會被 409 擋掉——一條把自己的訊息永久鎖死的路。串流結束
    時本來就只會寫終局狀態，所以這裡沒有擋掉任何真實流程。
    """
    envelope = stream_envelope(metadata)
    if envelope is None:
        return metadata
    state = envelope.get("state")
    if state not in _STREAM_ALL_STATES:
        raise HTTPException(status_code=400, detail="串流狀態不合法")
    if state not in STREAM_TERMINAL_STATES:
        raise HTTPException(
            status_code=400,
            detail="串流狀態只能由預留流程建立，這裡只接受已結束的狀態",
        )
    if "writer" in envelope:
        cleaned = {k: v for k, v in envelope.items() if k != "writer"}
        return {**metadata, STREAM_META_KEY: cleaned}
    return metadata


def _validate_rating_score(rating: Optional[str], score: Optional[int]) -> None:
    """Enforce thumb↔score pairing: up→6–10, down→1–5; None score always ok."""
    if score is None:
        return
    if rating == "up":
        if not (6 <= score <= 10):
            raise HTTPException(
                status_code=400,
                detail="讚的分數須為 6–10",
            )
        return
    if rating == "down":
        if not (1 <= score <= 5):
            raise HTTPException(
                status_code=400,
                detail="爛的分數須為 1–5",
            )
        return
    raise HTTPException(
        status_code=400,
        detail="沒有拇指評分時不能給分數",
    )


def set_message_rating(
    db: Session,
    conv_id: int,
    message_id: int,
    user: User,
    rating: Optional[str],
    comment: Optional[str] = None,
    reasons: Optional[list] = None,
    rating_score: Optional[int] = None,
    *,
    score_provided: bool = False,
) -> Message:
    """Record thumbs-up/down feedback on an assistant message.

    Access is gated by conversation ownership (reusing get_conversation's
    _check_access). Only assistant messages are ratable — rating a user/system
    message is a client bug and 400s out. ``rating=None`` clears an existing
    rating (and its fine score), letting the UI toggle off.

    ``rating_score`` is optional. Omit it (``score_provided=False``) to leave
    an existing score alone when only attaching a comment; flip the thumb
    without a score and the old number is cleared so a stale half-scale
    value cannot sit under the wrong thumb.
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

    prev_rating = msg.rating
    if score_provided:
        _validate_rating_score(rating, rating_score)
        msg.rating_score = rating_score
    elif rating is None or prev_rating != rating:
        # Cleared, or thumb flipped / first set without a number.
        msg.rating_score = None

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


# ── Named shares (P4.3) ───────────────────────────────────────────────────────

def create_share(
    db: Session,
    conv_id: int,
    user: User,
    *,
    target_user_id: Optional[int] = None,
    target_username: Optional[str] = None,
    target_department_id: Optional[int] = None,
    target_department_name: Optional[str] = None,
    expires_at: Optional[datetime] = None,
) -> ConversationShare:
    from app.schemas.contracts.classification import (
        ClassificationLevel,
        classification_audit_required,
        outbound_action_allowed,
    )

    conv = get_conversation(db, conv_id, user, for_write=True)
    level = ClassificationLevel.from_storage(conv.classification_level)
    # SYSTEM-MAP §8 L241-242: allow iff level <= TRADE_SECRET; audit iff
    # level >= TRADE_SECRET (including the allow path for 營業秘密).
    if not outbound_action_allowed(level):
        raise HTTPException(
            status_code=403,
            detail=(
                f"此對話密等為「{level.to_storage()}」，超過營業秘密，不可分享。"
                "請改用密等較低的對話，或向管理員申請降密後再分享。"
            ),
        )

    resolved_user_id, resolved_dept_id = _resolve_share_target(
        db,
        target_user_id=target_user_id,
        target_username=target_username,
        target_department_id=target_department_id,
        target_department_name=target_department_name,
    )

    # Reject sharing to self as a person (unit share covering own dept is OK).
    if resolved_user_id is not None and resolved_user_id == user.id:
        raise HTTPException(
            status_code=400,
            detail="不能分享給自己。請指定其他帳號或單位。",
        )

    existing = _find_active_share(
        db,
        conversation_id=conv.id,
        target_user_id=resolved_user_id,
        target_department_id=resolved_dept_id,
    )
    if existing is not None:
        if existing.expires_at and _as_utc(existing.expires_at) < datetime.now(
            timezone.utc
        ):
            db.delete(existing)
            db.flush()
        else:
            raise HTTPException(
                status_code=409,
                detail=(
                    "已分享給此對象。若要調整權限，請先撤銷既有分享後再建一次。"
                ),
            )

    share = ConversationShare(
        conversation_id=conv.id,
        target_user_id=resolved_user_id,
        target_department_id=resolved_dept_id,
        expires_at=expires_at,
        created_by=user.id,
    )
    db.add(share)
    target_label = _share_target_label(db, share)
    if classification_audit_required(level):
        log_audit_event(
            db,
            action="share_conversation",
            resource_type="conversation",
            actor=user,
            resource_id=conv_id,
            detail=(
                f"User {user.username} shared conversation {conv_id} "
                f"with {target_label} at level {level.to_storage()}"
            ),
            metadata={
                "classification_level": level.to_storage(),
                "target_user_id": resolved_user_id,
                "target_department_id": resolved_dept_id,
            },
        )
    db.commit()
    db.refresh(share)
    # Re-load with target relationships for ShareOut enrichment.
    from sqlalchemy.orm import joinedload

    return (
        db.query(ConversationShare)
        .options(
            joinedload(ConversationShare.target_user),
            joinedload(ConversationShare.target_department),
        )
        .filter(ConversationShare.id == share.id)
        .one()
    )


def list_shares(db: Session, conv_id: int, user: User) -> list[ConversationShare]:
    get_conversation(db, conv_id, user, for_write=True)
    from sqlalchemy.orm import joinedload

    return (
        db.query(ConversationShare)
        .options(
            joinedload(ConversationShare.target_user),
            joinedload(ConversationShare.target_department),
        )
        .filter(ConversationShare.conversation_id == conv_id)
        .order_by(ConversationShare.id)
        .all()
    )


def revoke_share(db: Session, conv_id: int, share_id: int, user: User) -> None:
    share = db.query(ConversationShare).filter(ConversationShare.id == share_id).first()
    if not share or share.conversation_id != conv_id:
        raise HTTPException(status_code=404, detail="找不到此分享。請重新整理分享清單。")
    get_conversation(db, share.conversation_id, user, for_write=True)
    db.delete(share)
    db.commit()


def _as_utc(dt: datetime) -> datetime:
    """Normalize stored expiry to aware UTC for comparison.

    Thin wrapper over ``app.time_utils.as_utc`` — same naive-UTC convention
    as ``api_key_service._as_utc``. Safe after the column is ``timestamptz``.
    """
    from app.time_utils import as_utc

    out = as_utc(dt)
    assert out is not None
    return out


def _share_expired(share: ConversationShare, *, now: datetime | None = None) -> bool:
    if share.expires_at is None:
        return False
    ref = now if now is not None else datetime.now(timezone.utc)
    return _as_utc(share.expires_at) < ref


def user_has_active_share(db: Session, conv: Conversation, user: User) -> bool:
    """True when an unexpired named share grants ``user`` read access now.

    Department shares expand via ``_department_scope_ids`` at read time so
    re-parenting after the share was created is honoured.
    """
    if not user.is_active:
        return False
    now = datetime.now(timezone.utc)
    shares = (
        db.query(ConversationShare)
        .filter(ConversationShare.conversation_id == conv.id)
        .all()
    )
    for share in shares:
        if _share_expired(share, now=now):
            continue
        if share.target_user_id is not None:
            if share.target_user_id == user.id:
                return True
            continue
        if share.target_department_id is None:
            continue
        if user.department_id is None:
            continue
        scope = _department_scope_ids(db, share.target_department_id)
        if scope is not None and user.department_id in scope:
            return True
    return False


def _shared_conversation_ids_for_user(db: Session, user: User) -> list[int]:
    from app.schemas.contracts.classification import (
        ClassificationLevel,
        outbound_action_allowed,
    )

    if not user.is_active:
        return []
    now = datetime.now(timezone.utc)
    ids: set[int] = set()
    # Person-targeted shares.
    person_rows = (
        db.query(ConversationShare.conversation_id)
        .filter(ConversationShare.target_user_id == user.id)
        .all()
    )
    person_conv_ids = {row[0] for row in person_rows}
    # Department-targeted: filter in Python so scope expands at read time.
    dept_shares = (
        db.query(ConversationShare)
        .filter(ConversationShare.target_department_id.isnot(None))
        .all()
    )
    for share in dept_shares:
        if _share_expired(share, now=now):
            continue
        if user.department_id is None:
            continue
        scope = _department_scope_ids(db, share.target_department_id)
        if scope is not None and user.department_id in scope:
            ids.add(share.conversation_id)
    # Apply expiry to person shares too.
    if person_conv_ids:
        person_shares = (
            db.query(ConversationShare)
            .filter(
                ConversationShare.target_user_id == user.id,
                ConversationShare.conversation_id.in_(person_conv_ids),
            )
            .all()
        )
        for share in person_shares:
            if _share_expired(share, now=now):
                continue
            ids.add(share.conversation_id)
    if not ids:
        return []
    # Drop shares whose conversation has latched above the outbound ceiling
    # (create-time gate alone is not enough after later classify/latch).
    allowed: list[int] = []
    for conv in (
        db.query(Conversation).filter(Conversation.id.in_(ids)).all()
    ):
        level = ClassificationLevel.from_storage(conv.classification_level)
        if outbound_action_allowed(level):
            allowed.append(conv.id)
    return sorted(allowed)


def _resolve_share_target(
    db: Session,
    *,
    target_user_id: Optional[int],
    target_username: Optional[str],
    target_department_id: Optional[int],
    target_department_name: Optional[str],
) -> tuple[Optional[int], Optional[int]]:
    """Resolve exactly one named target; fail closed with actionable detail."""
    user_keys = sum(
        1 for v in (target_user_id, (target_username or "").strip() or None) if v
    )
    dept_keys = sum(
        1
        for v in (
            target_department_id,
            (target_department_name or "").strip() or None,
        )
        if v
    )
    if user_keys + dept_keys == 0:
        raise HTTPException(
            status_code=400,
            detail=(
                "請指定分享對象：輸入對方帳號，或指定一個單位。"
                "匿名連結已停用。"
            ),
        )
    if user_keys > 0 and dept_keys > 0:
        raise HTTPException(
            status_code=400,
            detail="一次只能分享給「一個人」或「一個單位」，請只填其中一種。",
        )
    if user_keys > 1 or dept_keys > 1:
        raise HTTPException(
            status_code=400,
            detail="請只提供一種識別方式（帳號或 id；單位名稱或 id）。",
        )

    if target_user_id is not None or (target_username or "").strip():
        target = _resolve_target_user(
            db, user_id=target_user_id, username=target_username
        )
        return target.id, None

    target_dept = _resolve_target_department(
        db, department_id=target_department_id, name=target_department_name
    )
    return None, target_dept.id


def _resolve_target_user(
    db: Session, *, user_id: Optional[int], username: Optional[str]
) -> User:
    if user_id is not None:
        target = db.query(User).filter(User.id == user_id).first()
        label = f"id={user_id}"
    else:
        label = (username or "").strip()
        target = db.query(User).filter(User.username == label).first()

    if target is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"找不到帳號「{label}」。請確認對方帳號拼寫，"
                "或改分享給單位。"
            ),
        )
    if not target.is_active:
        raise HTTPException(
            status_code=400,
            detail=(
                f"帳號「{target.username}」已停用，無法分享。"
                "請改選其他人或單位。"
            ),
        )
    return target


def _resolve_target_department(
    db: Session, *, department_id: Optional[int], name: Optional[str]
) -> Department:
    if department_id is not None:
        dept = db.query(Department).filter(Department.id == department_id).first()
        label = f"id={department_id}"
    else:
        label = (name or "").strip()
        dept = db.query(Department).filter(Department.name == label).first()

    if dept is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"找不到單位「{label}」。請確認單位名稱，"
                "或改分享給指定人。"
            ),
        )
    if not dept.is_active:
        raise HTTPException(
            status_code=400,
            detail=(
                f"單位「{dept.name}」已停用，無法分享。"
                "請改選其他單位或指定人。"
            ),
        )
    return dept


def _find_active_share(
    db: Session,
    *,
    conversation_id: int,
    target_user_id: Optional[int],
    target_department_id: Optional[int],
) -> Optional[ConversationShare]:
    q = db.query(ConversationShare).filter(
        ConversationShare.conversation_id == conversation_id
    )
    if target_user_id is not None:
        q = q.filter(ConversationShare.target_user_id == target_user_id)
    else:
        q = q.filter(
            ConversationShare.target_department_id == target_department_id
        )
    return q.first()


def _share_target_label(db: Session, share: ConversationShare) -> str:
    if share.target_user_id is not None:
        u = db.query(User).filter(User.id == share.target_user_id).first()
        return f"user:{u.username}" if u else f"user_id:{share.target_user_id}"
    d = (
        db.query(Department)
        .filter(Department.id == share.target_department_id)
        .first()
    )
    return f"department:{d.name}" if d else f"department_id:{share.target_department_id}"


# ── Internal helpers ──────────────────────────────────────────────────────────

def _check_access(conv: Conversation, user: User) -> None:
    """Owner-or-admin write gate (mutations, share management).

    Unauthorised collapses to the same 404 as missing (models pattern).
    """
    if is_admin_tier(user):
        return
    if conv.user_id != user.id:
        raise HTTPException(status_code=404, detail="找不到此對話")


def _check_read_access(db: Session, conv: Conversation, user: User) -> None:
    """Owner, admin, or active named-share recipient (P4.3).

    Share recipients additionally need the conversation still within the
    outbound ceiling (≤營業秘密). Create-time gating alone would leave a
    hole after a later classify / agent latch to 密／機密.

    No relationship to the conversation → 404 (existence not an oracle).
    Active share but over ceiling → 403 with a self-help message: the
    recipient already knows the share exists, so distinguishing is useful.
    """
    from app.schemas.contracts.classification import (
        ClassificationLevel,
        outbound_action_allowed,
    )

    if is_admin_tier(user):
        return
    if conv.user_id == user.id:
        return
    if not user_has_active_share(db, conv, user):
        raise HTTPException(status_code=404, detail="找不到此對話")
    level = ClassificationLevel.from_storage(conv.classification_level)
    if not outbound_action_allowed(level):
        raise HTTPException(
            status_code=403,
            detail=(
                f"此對話密等為「{level.to_storage()}」，超過營業秘密，"
                "分享對象不可再讀取。請向擁有者確認是否另開較低密等的對話，"
                "或請擁有者撤銷此分享。"
            ),
        )

