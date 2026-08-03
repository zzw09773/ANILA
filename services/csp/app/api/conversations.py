"""Conversation management endpoints (JWT auth)."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, object_session

from app.api.auth import get_current_user
from app.database import get_db
from app.models.attachment import Attachment
from app.models.audit_log import AuditLog
from app.models.conversation import Conversation, ConversationShare, ConversationUserMeta
from app.models.message import Message
from app.models.user import User
from app.services import conversation_service as svc
from app.services import message_tree as mtree
from app.services.auth_service import is_admin_tier
from app.schemas.base import ApiResponseModel

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
    """Partial update: title (owner) and/or the caller's personal meta.

    ``tags`` are user-authored only — the derived ``classified`` tag is
    stripped server-side and reattached from ``conversation.classified``.
    """

    title: Optional[str] = Field(None, max_length=255)
    starred: Optional[bool] = None
    folder: Optional[str] = Field(None, max_length=64)
    tags: Optional[list[Annotated[str, Field(max_length=40)]]] = Field(
        None, max_length=32,
    )


class AttachmentOut(BaseModel):
    reference_id: str
    filename: str
    content_type: str
    size_bytes: int
    model_config = {"from_attributes": True}


class MessageOut(ApiResponseModel):
    id: int
    role: str
    content: str
    # OW-1 tree fields (docs/plans/ow1-message-tree-blueprint.md).
    parent_id: Optional[int] = None
    sibling_index: int = 0
    sibling_count: int = 1
    sibling_ids: list[int] = []
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
    # Fine score beside the thumb (up→6–10, down→1–5); None = thumb only.
    rating_score: Optional[int] = None
    created_at: datetime
    attachments: list[AttachmentOut] = []
    model_config = {"from_attributes": True, "populate_by_name": True}


# Upper bound on a single message body. ~500KB ≈ 125k tokens — far above any
# legit completion or paste, but stops an authenticated insider from amplifying
# writes into multi-MB rows.
_MAX_MSG_CHARS = 500_000


class MessageRatingUpdate(BaseModel):
    rating: Optional[str] = Field(None, pattern="^(up|down)$")
    # Optional fine score. Pairing with the thumb is enforced in the service
    # (up→6–10, down→1–5); omit the field to leave an existing score alone
    # (e.g. when only attaching a down-vote comment).
    rating_score: Optional[int] = Field(None, ge=1, le=10)
    # Structured feedback (optional, usually accompanies a 'down' rating).
    # Air-gapped deployments rely on this as the main model-quality signal.
    comment: Optional[str] = Field(None, max_length=2000)
    reasons: Optional[list[Annotated[str, Field(max_length=200)]]] = Field(
        None, max_length=20
    )


class MessageUpdate(BaseModel):
    """In-place patch payload for ``PUT /messages/{id}``.

    Only the supplied fields are written; ``None`` leaves them untouched.
    """
    content: Optional[str] = Field(None, max_length=_MAX_MSG_CHARS)
    trace_id: Optional[str] = None
    latency_ms: Optional[int] = None
    model_name: Optional[str] = None
    agent_name: Optional[str] = None
    metadata: Optional[dict] = None
    # 預留列的寫入者權杖。只有那一列還在 reserved/streaming 時才會被檢查；
    # 一般的 in-place patch（ANILALM finalize 等）不必帶。
    stream_writer: Optional[str] = Field(None, max_length=200)


class ReserveReplyCreate(BaseModel):
    """Reserve an empty assistant row under a user message, before streaming."""
    # 由前端產生的隨機權杖；持有者才能把內容寫進這一列。
    stream_writer: str = Field(..., min_length=8, max_length=200)
    model_name: Optional[str] = None
    agent_name: Optional[str] = None


class TurnHeadCreate(BaseModel):
    """Append the user message and reserve its assistant row in one request."""
    content: str = Field(..., max_length=_MAX_MSG_CHARS)
    stream_writer: str = Field(..., min_length=8, max_length=200)
    model_name: Optional[str] = None
    agent_name: Optional[str] = None


class ConversationOut(ApiResponseModel):
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
    # render a different banner ("此對話因引用過往加密記憶而升級為列管"
    # vs the existing "此對話為列管"). Always FALSE on rows pre-dating
    # migration 0031, so old data renders as before.
    classification_inherited: bool = False
    # Slice 3b: four-level classification (SYSTEM-MAP §8). Additive — the legacy
    # ``classified`` bool is retained as a compatibility read model for older
    # UI (mirror rule: classified = classification_level >= 密 / RESTRICTED;
    # preserves old rank-2 controlled-set semantics). Defaults to
    # 無機密 so rows pre-dating the four-level column render unclassified.
    classification_level: str = "無機密"
    # Per-caller view (conversation_user_meta). Defaults keep list/get
    # working when the caller has never organised this thread.
    starred: bool = False
    folder: str = "all"
    tags: list[str] = []
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class ConversationDetail(ConversationOut):
    active_leaf_message_id: Optional[int] = None
    messages: list[MessageOut] = []


class ConversationPathOut(BaseModel):
    """Active path after leaf switch or subtree delete (OW-1)."""
    active_leaf_message_id: Optional[int] = None
    messages: list[MessageOut] = []


class TurnHeadOut(BaseModel):
    """Both rows created by ``POST /{conv_id}/turn`` — they are one unit."""
    user: MessageOut
    assistant: MessageOut


class MessageAppend(BaseModel):
    role: str = Field(..., pattern="^(user|assistant|system|tool)$")
    content: str = Field(..., max_length=_MAX_MSG_CHARS)
    trace_id: Optional[str] = None
    latency_ms: Optional[int] = None
    model_name: Optional[str] = None
    agent_name: Optional[str] = None
    metadata: Optional[dict] = None
    # OW-1: omitted/null → thread onto active leaf; explicit int → branchable gate.
    parent_id: Optional[int] = None
    set_active: bool = True


class MessageBranchCreate(BaseModel):
    role: str = Field(..., pattern="^(user|assistant|system|tool)$")
    content: str = Field(..., max_length=_MAX_MSG_CHARS)
    trace_id: Optional[str] = None
    latency_ms: Optional[int] = None
    model_name: Optional[str] = None
    agent_name: Optional[str] = None
    metadata: Optional[dict] = None
    set_active: bool = True


class ActiveLeafUpdate(BaseModel):
    message_id: int


class ShareCreate(BaseModel):
    """P4.3 — share to exactly one named person XOR one department unit."""

    target_username: Optional[str] = Field(None, max_length=100)
    target_user_id: Optional[int] = Field(None, ge=1)
    target_department_id: Optional[int] = Field(None, ge=1)
    target_department_name: Optional[str] = Field(None, max_length=100)
    # ``mode`` / ``allow_fork`` 已移除:兩者從來沒有被任何授權判定讀過,
    # 分享一律唯讀。收下再丟掉的欄位比沒有這個欄位更危險 —— 它讓呼叫端
    # 以為自己設定了權限。舊前端仍會送這兩個鍵,Pydantic 預設
    # ``extra='ignore'`` 會直接忽略,不會 422。
    expires_at: Optional[datetime] = None


class ShareOut(ApiResponseModel):
    id: int
    target_user_id: Optional[int] = None
    target_username: Optional[str] = None
    target_department_id: Optional[int] = None
    target_department_name: Optional[str] = None
    expires_at: Optional[datetime]
    created_at: datetime
    model_config = {"from_attributes": True}


def _share_out(share: ConversationShare) -> ShareOut:
    """Enrich share row with target display names for the owner UI."""
    username = None
    if share.target_user is not None:
        username = share.target_user.username
    elif share.target_user_id is not None:
        username = None
    dept_name = None
    if share.target_department is not None:
        dept_name = share.target_department.name
    return ShareOut(
        id=share.id,
        target_user_id=share.target_user_id,
        target_username=username,
        target_department_id=share.target_department_id,
        target_department_name=dept_name,
        expires_at=share.expires_at,
        created_at=share.created_at,
    )


def _attachments_for_sibling_group(
    session: Session,
    sibling_ids: list[int],
    conversation_id: int,
) -> list[Attachment]:
    """Attachments bound to any message sharing the same parent_id group.

    Display-only: one Attachment row per upload; no clone / re-point.
    ``conversation_id`` is defence-in-depth — callers already scope via
    ``get_conversation`` + ``mtree.load_edges(conv.id)``, but the query must
    not rely on that discipline alone.
    Extracted so tests can mutate the lookup and prove the chip path.
    """
    return (
        session.query(Attachment)
        .filter(
            Attachment.conversation_id == conversation_id,
            Attachment.message_id.in_(sibling_ids),
        )
        .order_by(Attachment.created_at.asc(), Attachment.id.asc())
        .all()
    )


def _message_out(msg: Message, sibling_ids: list[int] | None = None) -> MessageOut:
    """Build MessageOut with derived sibling nav fields.

    When the message belongs to a multi-member sibling group (same
    ``parent_id``, via ``mtree.sibling_groups``), surface every attachment
    bound to any sibling so branch chips match the turn. Solo messages keep
    the ORM ``msg.attachments`` relationship unchanged.
    """
    ids = sibling_ids if sibling_ids is not None else [msg.id]
    try:
        index = ids.index(msg.id)
    except ValueError:
        index = 0
        ids = [msg.id]
    base = MessageOut.model_validate(msg)
    update: dict = {
        "parent_id": msg.parent_id,
        "sibling_index": index,
        "sibling_count": len(ids),
        "sibling_ids": ids,
    }
    # Symmetric union is deliberate: siblings are two versions of the same
    # question, so their chips must agree. Solo → leave ORM attachments alone.
    if len(ids) > 1:
        session = object_session(msg)
        if session is not None:
            rows = _attachments_for_sibling_group(
                session, ids, msg.conversation_id,
            )
            update["attachments"] = [
                AttachmentOut.model_validate(a) for a in rows
            ]
    return base.model_copy(update=update)


def _enrich_message_list(
    messages: list[Message], edges: list[tuple[int, int | None]],
) -> list[MessageOut]:
    groups = mtree.sibling_groups(edges)
    return [
        _message_out(msg, groups.get(msg.parent_id, [msg.id]))
        for msg in messages
    ]


def _enrich_out(
    conv: Conversation,
    meta: ConversationUserMeta | None = None,
) -> dict:
    data = ConversationOut.model_validate(conv).model_dump()
    data.update(svc.meta_view(conv, meta))
    return data


def _conversation_out(
    db: Session, user: User, conv: Conversation,
) -> ConversationOut:
    meta = svc.get_user_meta(db, user.id, conv.id)
    return ConversationOut(**_enrich_out(conv, meta))


def _conversation_detail(
    db: Session,
    conv: Conversation,
    *,
    view: str,
    user: User,
) -> ConversationDetail:
    edges = mtree.load_edges(db, conv.id)
    if view == "all":
        messages = svc._all_messages_ordered(db, conv.id)
    else:
        messages = svc.load_active_path(db, conv)
    meta = svc.get_user_meta(db, user.id, conv.id)
    data = _enrich_out(conv, meta)
    data["active_leaf_message_id"] = conv.active_leaf_message_id
    data["messages"] = _enrich_message_list(messages, edges)
    return ConversationDetail(**data)


def _path_out(
    db: Session, conv: Conversation, messages: list[Message],
) -> ConversationPathOut:
    edges = mtree.load_edges(db, conv.id)
    return ConversationPathOut(
        active_leaf_message_id=conv.active_leaf_message_id,
        messages=_enrich_message_list(messages, edges),
    )


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
    rows = svc.list_conversations(
        db, current_user,
        origin=origin,
        exclude_origin=exclude_origin,
        collection_id=collection_id,
    )
    # One batch query for the caller's meta — keeps the sidebar O(1) extra.
    metas = svc.load_user_metas(db, current_user.id, [c.id for c in rows])
    return [
        ConversationOut(**_enrich_out(c, metas.get(c.id)))
        for c in rows
    ]


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
    conv = svc.create_conversation(
        db,
        current_user.id,
        title=body.title,
        agent_id=body.agent_id,
        origin=origin,
        collection_id=body.collection_id,
    )
    return _conversation_out(db, current_user, conv)


class AdoptCompareRequest(BaseModel):
    """Promote one compare-mode answer into a persisted conversation.

    Defined here (API layer) rather than ``app/schemas`` so this ticket can
    land without touching the timezone package's schema rewrite.
    """

    title: str = Field("採用比較結果", max_length=255)
    # Prefer agent_name (data-plane id / unique Agent.name). Numeric agent_id
    # is accepted when the caller already knows the inventory PK.
    agent_name: Optional[str] = Field(default=None, max_length=100)
    agent_id: Optional[int] = Field(default=None, ge=1)
    origin: Optional[str] = Field(default="anila-ui", max_length=32)
    user_content: str = Field(..., max_length=_MAX_MSG_CHARS)
    assistant_content: str = Field(..., max_length=_MAX_MSG_CHARS)
    assistant_metadata: Optional[dict] = None
    assistant_trace_id: Optional[str] = None
    assistant_latency_ms: Optional[int] = None
    assistant_agent_name: Optional[str] = Field(default=None, max_length=255)


@router.post("/adopt", response_model=ConversationDetail, status_code=201)
def adopt_compare_answer(
    body: AdoptCompareRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Promote a compare answer into a real conversation (message tree + latch).

    Compare mode itself stays ephemeral; only the adopted column is written.
    Classification comes from the resolved agent's policy on the server —
    the client must not invent ``classified``.
    """
    origin = body.origin or "anila-ui"
    if origin == "anilalm":
        raise HTTPException(
            status_code=400,
            detail="比較採用僅支援 ANILA UI 對話（origin 不可為 anilalm）",
        )
    if not (body.user_content or "").strip():
        raise HTTPException(status_code=400, detail="採用內容缺少使用者訊息")
    if not (body.assistant_content or "").strip():
        raise HTTPException(status_code=400, detail="採用內容缺少助理訊息")

    conv = svc.adopt_compare_answer(
        db,
        current_user,
        title=body.title,
        user_content=body.user_content,
        assistant_content=body.assistant_content,
        agent_id=body.agent_id,
        agent_name=body.agent_name,
        origin=origin,
        assistant_metadata=body.assistant_metadata,
        assistant_trace_id=body.assistant_trace_id,
        assistant_latency_ms=body.assistant_latency_ms,
        assistant_agent_name=body.assistant_agent_name,
    )
    # Re-load so classification latch + active_leaf are visible in the
    # response the client treats as source of truth.
    conv = svc.get_conversation(db, conv.id, current_user)
    return _conversation_detail(db, conv, view="active", user=current_user)


class ConversationSearchHit(ConversationOut):
    # First matching message excerpt, for the search results list.
    snippet: Optional[str] = None


@router.get("/search", response_model=list[ConversationSearchHit])
def search_conversations(
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(30, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Full-text-ish search over the caller's own conversations — matches the
    title AND message content (ILIKE substring, which handles Chinese without a
    CJK tokenizer; pg_trgm GIN index makes it fast). Defined BEFORE /{conv_id}
    so 'search' isn't shadowed by the int path param.

    OE-4: message snippets follow the outbound block line (level ≥ 密 →
    snippet stays None). When a snippet is produced for a conversation at
    level ≥ 營業秘密, one read-audit row is written (same semantics as GET
    /{conv_id}; at most one per conversation per request).
    """
    like = f"%{q}%"
    # Conversations of this user whose title matches, OR which contain a
    # matching message. distinct on conversation id, newest first.
    matched_ids = (
        db.query(Conversation.id)
        .outerjoin(Message, Message.conversation_id == Conversation.id)
        .filter(Conversation.user_id == current_user.id)
        .filter((Conversation.title.ilike(like)) | (Message.content.ilike(like)))
        .distinct()
        .limit(limit)
        .subquery()
    )
    convs = (
        db.query(Conversation)
        .filter(Conversation.id.in_(matched_ids))
        .order_by(Conversation.updated_at.desc())
        .all()
    )
    from app.schemas.contracts.classification import (
        ClassificationLevel,
        classification_audit_required,
        outbound_action_allowed,
    )
    hits: list[dict] = []
    audits_pending = False
    metas = svc.load_user_metas(db, current_user.id, [c.id for c in convs])
    for c in convs:
        snippet = None
        # OE-4: snippet redaction follows outbound block line (level >=
        # RESTRICTED / 密); SYSTEM-MAP §8 L241. Boolean is display-only.
        level = ClassificationLevel.from_storage(c.classification_level)
        if outbound_action_allowed(level):
            msg = (
                db.query(Message)
                .filter(Message.conversation_id == c.id, Message.content.ilike(like))
                .order_by(Message.id)
                .first()
            )
            if msg and msg.content:
                idx = msg.content.lower().find(q.lower())
                start = max(0, idx - 20)
                snippet = ("…" if start > 0 else "") + msg.content[start:start + 80].strip()
        # L242 read-audit when snippet content is actually exposed.
        # Same AuditLog shape as log_classified_access; defer commit to
        # one WAL fsync after the loop (interactive sidebar, limit≤100).
        if snippet is not None and classification_audit_required(level):
            db.add(AuditLog(
                actor_user_id=current_user.id,
                actor_username=current_user.username,
                action="access_classified_conversation",
                resource_type="conversation",
                resource_id=str(c.id),
                status="success",
                detail=(
                    f"User {current_user.username} accessed classified "
                    f"conversation {c.id}"
                ),
            ))
            audits_pending = True
        meta = metas.get(c.id)
        data = _enrich_out(c, meta)
        data["snippet"] = snippet
        hits.append(data)
    if audits_pending:
        db.commit()
    return hits


@router.get("/{conv_id}", response_model=ConversationDetail)
def get_conversation(
    conv_id: int,
    view: str = Query("active", pattern="^(active|all)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.schemas.contracts.classification import (
        ClassificationLevel,
        classification_audit_required,
    )

    conv = svc.get_conversation(db, conv_id, current_user, for_write=False)
    # SYSTEM-MAP §8 L242:要落稽核 = 密等 ≥ 營業秘密 (read-audit).
    level = ClassificationLevel.from_storage(conv.classification_level)
    if classification_audit_required(level):
        svc.log_classified_access(db, conv_id, current_user)
    # OW-1 / P4.3: named-share recipients only see the active path (same
    # ceiling the retired public-share used). Owners/admins keep view=all.
    effective_view = view
    if (
        view == "all"
        and not is_admin_tier(current_user)
        and conv.user_id != current_user.id
    ):
        effective_view = "active"
    return _conversation_detail(
        db, conv, view=effective_view, user=current_user,
    )


@router.put("/{conv_id}", response_model=ConversationOut)
def update_conversation(
    conv_id: int,
    body: ConversationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conv = svc.update_conversation(
        db,
        conv_id,
        current_user,
        title=body.title,
        starred=body.starred,
        folder=body.folder,
        tags=body.tags,
    )
    return _conversation_out(db, current_user, conv)


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
    # parent_id explicitly supplied (including intentional fork) → branchable gate.
    explicit = "parent_id" in body.model_fields_set and body.parent_id is not None
    # §6-3 zh-TW 正規化在 conversation_service 共用落庫邊界（append/branch/update）
    msg = svc.append_message(
        db, conv_id, current_user,
        role=body.role,
        content=body.content,
        trace_id=body.trace_id,
        latency_ms=body.latency_ms,
        model_name=body.model_name,
        agent_name=body.agent_name,
        metadata=body.metadata,
        parent_id=body.parent_id,
        parent_id_explicit=explicit,
        set_active=body.set_active,
    )
    edges = mtree.load_edges(db, conv_id)
    groups = mtree.sibling_groups(edges)
    return _message_out(msg, groups.get(msg.parent_id, [msg.id]))


@router.post("/{conv_id}/turn", response_model=TurnHeadOut, status_code=201)
def start_turn(
    conv_id: int,
    body: TurnHeadCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Persist the user message and reserve its assistant row atomically.

    這是聊天送出路徑的第一步，取代「POST /messages 之後再 POST
    /reserve-reply」的兩次往返。兩次往返之間的 RTT 是一個窗口：兩個
    分頁同一瞬間按 Enter 時，後到的 append 會掛在前一則使用者訊息底下，
    前一個分頁的 reserve 隨即 409，那則使用者訊息就永遠拿不到答案。
    合成一次之後兩件事在同一個交易裡完成，這個窗口不存在。
    """
    user_msg, assistant_msg = svc.start_turn(
        db, conv_id, current_user,
        content=body.content,
        writer=body.stream_writer,
        model_name=body.model_name,
        agent_name=body.agent_name,
    )
    edges = mtree.load_edges(db, conv_id)
    groups = mtree.sibling_groups(edges)
    return TurnHeadOut(
        user=_message_out(user_msg, groups.get(user_msg.parent_id, [user_msg.id])),
        assistant=_message_out(
            assistant_msg, groups.get(assistant_msg.parent_id, [assistant_msg.id]),
        ),
    )


@router.post(
    "/{conv_id}/messages/{message_id}/reserve-reply",
    response_model=MessageOut,
    status_code=201,
)
def reserve_reply(
    conv_id: int,
    message_id: int,
    body: ReserveReplyCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Reserve the assistant row under ``message_id`` before streaming starts.

    這是「先落庫再串流」的第一步：助理訊息先有 id 和固定的 parent，
    active leaf 隨即前進到它身上，所以串流期間再送出的使用者訊息會正確
    掛在它底下，而不是變成前一則使用者訊息的同層兄弟。
    """
    msg = svc.reserve_assistant_reply(
        db, conv_id, message_id, current_user,
        writer=body.stream_writer,
        model_name=body.model_name,
        agent_name=body.agent_name,
    )
    edges = mtree.load_edges(db, conv_id)
    groups = mtree.sibling_groups(edges)
    return _message_out(msg, groups.get(msg.parent_id, [msg.id]))


@router.post(
    "/{conv_id}/messages/{message_id}/branch",
    response_model=MessageOut,
    status_code=201,
)
def branch_message(
    conv_id: int,
    message_id: int,
    body: MessageBranchCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    msg = svc.branch_message(
        db, conv_id, message_id, current_user,
        role=body.role,
        content=body.content,
        trace_id=body.trace_id,
        latency_ms=body.latency_ms,
        model_name=body.model_name,
        agent_name=body.agent_name,
        metadata=body.metadata,
        set_active=body.set_active,
    )
    edges = mtree.load_edges(db, conv_id)
    groups = mtree.sibling_groups(edges)
    return _message_out(msg, groups.get(msg.parent_id, [msg.id]))


@router.put("/{conv_id}/active-leaf", response_model=ConversationPathOut)
def set_active_leaf(
    conv_id: int,
    body: ActiveLeafUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conv, path = svc.set_active_leaf(db, conv_id, current_user, body.message_id)
    return _path_out(db, conv, path)


@router.delete(
    "/{conv_id}/messages/{message_id}",
    response_model=ConversationPathOut,
)
def delete_message_branch(
    conv_id: int,
    message_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conv, path = svc.delete_message_branch(db, conv_id, message_id, current_user)
    return _path_out(db, conv, path)


@router.put("/{conv_id}/messages/{message_id}/rating", response_model=MessageOut)
def set_message_rating(
    conv_id: int,
    message_id: int,
    body: MessageRatingUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Record thumbs-up/down on an assistant message, or clear with rating=null.
    Optionally attaches a fine score and structured feedback (comment + reasons)."""
    msg = svc.set_message_rating(
        db, conv_id, message_id, current_user, body.rating,
        rating_score=body.rating_score,
        score_provided="rating_score" in body.model_fields_set,
        comment=body.comment, reasons=body.reasons,
    )
    edges = mtree.load_edges(db, conv_id)
    groups = mtree.sibling_groups(edges)
    return _message_out(msg, groups.get(msg.parent_id, [msg.id]))


@router.put("/{conv_id}/messages/{message_id}", response_model=MessageOut)
def update_message(
    conv_id: int,
    message_id: int,
    body: MessageUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Patch an existing message (in-place, non-truncating).

    Used for ANILALM finalize and metadata patches. ANILA regenerate forks
    via POST .../branch (OW-1); this path is no longer the regenerate write.
    """
    msg = svc.update_message_content(
        db, conv_id, message_id, current_user,
        content=body.content,
        trace_id=body.trace_id,
        latency_ms=body.latency_ms,
        model_name=body.model_name,
        agent_name=body.agent_name,
        metadata=body.metadata,
        stream_writer=body.stream_writer,
    )
    edges = mtree.load_edges(db, conv_id)
    groups = mtree.sibling_groups(edges)
    return _message_out(msg, groups.get(msg.parent_id, [msg.id]))


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
    conv = svc.classify_conversation(db, conv_id, current_user)
    return _conversation_out(db, current_user, conv)


# ── Named shares (P4.3) ───────────────────────────────────────────────────────

@router.get("/{conv_id}/shares", response_model=list[ShareOut])
def list_shares(
    conv_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return [_share_out(s) for s in svc.list_shares(db, conv_id, current_user)]


@router.post("/{conv_id}/shares", response_model=ShareOut, status_code=201)
def create_share(
    conv_id: int,
    body: ShareCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    share = svc.create_share(
        db, conv_id, current_user,
        target_user_id=body.target_user_id,
        target_username=body.target_username,
        target_department_id=body.target_department_id,
        target_department_name=body.target_department_name,
        expires_at=body.expires_at,
    )
    return _share_out(share)


@router.delete("/{conv_id}/shares/{share_id}", status_code=204)
def revoke_share(
    conv_id: int,
    share_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc.revoke_share(db, conv_id, share_id, current_user)
