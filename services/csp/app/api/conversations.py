"""Conversation management endpoints (JWT auth)."""
from __future__ import annotations

from app.api.ingestion.surface import SURFACE_ANILALM
from app.schemas.base import ApiResponseModel

from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models.artifact import ExportRecord
from app.models.attachment import Attachment
from app.models.conversation import Conversation, ConversationShare
from app.models.message import Message
from app.models.user import User
from app.schemas.contracts.classification import ClassificationLevel
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


class MessageOut(ApiResponseModel):
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


class MessageTreeOut(MessageOut):
    """Full-tree read (``?tree=1``): adds ``parent_id`` for client revision nav."""
    parent_id: Optional[int] = None



# Upper bound on a single message body. ~500KB ≈ 125k tokens — far above any
# legit completion or paste, but stops an authenticated insider from amplifying
# writes into multi-MB rows.
_MAX_MSG_CHARS = 500_000


class MessageRatingUpdate(BaseModel):
    rating: Optional[str] = Field(None, pattern="^(up|down)$")
    # Structured feedback (optional, usually accompanies a 'down' rating).
    # Air-gapped deployments rely on this as the main model-quality signal.
    comment: Optional[str] = Field(None, max_length=2000)
    reasons: Optional[list[Annotated[str, Field(max_length=200)]]] = Field(
        None, max_length=20
    )


class MessageEdit(BaseModel):
    content: str = Field(..., min_length=1, max_length=_MAX_MSG_CHARS)


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
    # render a different banner ("此對話因引用過往加密記憶而升級為機密"
    # vs the existing "此對話為機密"). Always FALSE on rows pre-dating
    # migration 0031, so old data renders as before.
    classification_inherited: bool = False
    # Slice 3b: five-level classification (doc 08 §1). Additive — the legacy
    # ``classified`` bool is retained as a compatibility read model for older
    # UI (mirror rule: classified = classification_level >= 機密). Defaults to
    # 無機密 so rows pre-dating the five-level column render unclassified.
    classification_level: str = "無機密"
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class ConversationDetail(ConversationOut):
    messages: list[MessageOut] = []


class ConversationTreeDetail(ConversationOut):
    """``?tree=1`` response: full message tree + active leaf pointer."""
    messages: list[MessageTreeOut] = []
    active_leaf_message_id: Optional[int] = None


class ActiveLeafUpdate(BaseModel):
    message_id: int = Field(..., ge=1)


class MessageAppend(BaseModel):
    role: str = Field(..., pattern="^(user|assistant|system|tool)$")
    content: str = Field(..., max_length=_MAX_MSG_CHARS)
    trace_id: Optional[str] = None
    latency_ms: Optional[int] = None
    model_name: Optional[str] = None
    agent_name: Optional[str] = None
    metadata: Optional[dict] = None
    # W2-3: where the new node hangs. Callers that mean "sibling" (retry /
    # regenerate recovery / re-run after an edit) MUST name it — the server
    # cannot tell those apart from "continue the current turn" and would
    # attach the row under the active leaf, which by then may have moved.
    #
    # Deliberately still OPTIONAL: this is the only message-creation endpoint,
    # so it also carries the ordinary next-user-turn case where the active-leaf
    # default IS the intended parent, and older shipped SPA bundles never send
    # the field. Requiring it would 422 every write from a client one deploy
    # behind — an availability regression worse than the topology bug it
    # prevents. The obligation is enforced client-side instead, where the
    # intent actually lives (``runtime/messageParent.js`` + its enumeration
    # test), because only the caller knows which of the two cases it means.
    parent_id: Optional[int] = None


class ShareCreate(BaseModel):
    mode: str = Field("read_only", pattern="^(read_only|fork)$")
    allow_fork: bool = False
    expires_at: Optional[datetime] = None


class ShareOut(ApiResponseModel):
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
    limit: Optional[int] = Query(
        default=None,
        ge=1,
        le=200,
        description=(
            "每頁列數。**不給就是舊行為**(回全部),舊 client 不受影響。"
            "有給時,若還有下一頁,回應頭 `X-Next-Cursor` 會帶下一頁的 cursor。"
        ),
    ),
    cursor: Optional[int] = Query(
        default=None,
        ge=1,
        description="上一頁回應頭 `X-Next-Cursor` 的值(= 上一頁最後一列的 id)。",
    ),
    response: Response = None,  # noqa: B008 — FastAPI 注入用
    current_user: User = Depends(get_current_user),
):
    if origin is not None and exclude_origin is not None:
        raise HTTPException(
            status_code=400,
            detail="origin and exclude_origin are mutually exclusive",
        )
    # W3-7e:cursor 分頁。**不帶 limit 就是舊行為**(全撈),所以舊 client 一個
    # 字都不用改。下一頁的 cursor 走 `X-Next-Cursor` 回應頭而不是塞進 body ——
    # JSON 形狀與 `response_model` 因此完全不變,不會讓契約長出第二個版本。
    rows, next_cursor = svc.list_conversations_page(
        db, current_user,
        origin=origin,
        exclude_origin=exclude_origin,
        collection_id=collection_id,
        limit=limit,
        cursor=cursor,
    )
    if next_cursor is not None:
        response.headers["X-Next-Cursor"] = str(next_cursor)
    return rows


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
        _require_collection_access(
            db, current_user, body.collection_id, origin=SURFACE_ANILALM
        )
    return svc.create_conversation(
        db,
        current_user.id,
        title=body.title,
        agent_id=body.agent_id,
        origin=origin,
        collection_id=body.collection_id,
    )


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

    Classified conversations are matched by title only — their message bodies
    are not exposed through search results (snippet stays None) so encrypted
    content doesn't leak into the sidebar.
    """
    like = f"%{q}%"
    # 標題命中,或含有命中訊息的對話 —— 最近的優先。
    #
    # ⚠ 先前的寫法有一個確定的缺陷:subquery 是 `.distinct().limit(limit)` 而
    # **沒有 order_by**,排序只發生在外層、對「已經被截斷的子集」做。SQL 語意上
    # LIMIT 沒有 ORDER BY 時回哪幾列是未定義的(PostgreSQL 通常給 scan 順序,
    # 約等於偏舊)。
    #
    # 使用者面的後果:用高頻詞(「報告」「特休」「簽核」)搜三個月前的對話,
    # 結果宣稱只有 30 筆而且很可能不含目標 → 使用者的結論是「找不到」,但資料
    # 就在庫裡。這是「三個月後想找回一則對話」最典型的失敗,而且是**靜默**的
    # (沒有錯誤、沒有「還有更多」的提示)。
    #
    # 修法刻意不是「把 order_by 加進原本的 subquery」—— 那在 PostgreSQL 會直接
    # 報錯:`SELECT DISTINCT` 的 ORDER BY 運算式必須出現在 select list 裡,而
    # select list 只有 id。改用 EXISTS 之後 outerjoin 與 DISTINCT 都不需要了
    # (join 產生重複 id 才是 DISTINCT 存在的唯一理由),LIMIT 直接作用在
    # 已排序的結果上,語意一目瞭然,而且 EXISTS 可以在找到第一筆命中訊息時
    # 就短路,不必展開整個 join。
    message_hit = (
        db.query(Message.id)
        .filter(
            Message.conversation_id == Conversation.id,
            Message.content.ilike(like),
        )
        .exists()
    )
    convs = (
        db.query(Conversation)
        .filter(Conversation.user_id == current_user.id)
        .filter(Conversation.title.ilike(like) | message_hit)
        .order_by(Conversation.updated_at.desc())
        .limit(limit)
        .all()
    )
    hits: list[dict] = []
    for c in convs:
        snippet = None
        # W1-1:原本是 `if not c.classified`。legacy boolean 的鏡射規則是
        # `classified = level >= 機密`(見 ConversationOut 的註解),所以**營業
        # 秘密的 classified 是 False** → 它的訊息內文會被擷取 80 字回傳。
        # 內容外洩,而且發生在「列表」這種最容易被截圖、最不會被注意的地方。
        # 改吃 is_controlled()(> 無機密即受控,未知值 fail-closed)。
        # 注意:對話本身仍然出現在結果裡,只是不附內文 —— 整條消失會讓使用者
        # 以為東西不見了(N-3 的禁令姿態要求)。
        if not svc.is_controlled(c):
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
        else:
            # 內文比對是一個「字串存在性 oracle」:開對話讀內容會落稽核
            # (W1-1①),但用搜尋逐字探測不會 —— 等於繞過讀取稽核做零紀錄
            # 的內容探測(PR #50 作者自陳 A2,2026-07-27 分診確認)。對話
            # 照樣出現在結果(N-3:整條消失會讓使用者以為東西不見了),但
            # **內文命中就落稽核**,與開對話讀取同一種帳。標題命中不記 ——
            # 標題是 metadata。查詢字串刻意不寫進稽核,免得稽核日誌自己
            # 變成洩漏面。
            content_matched = (
                db.query(Message.id)
                .filter(Message.conversation_id == c.id, Message.content.ilike(like))
                .first()
                is not None
            )
            if content_matched:
                svc.log_controlled_access(
                    db,
                    c.id,
                    current_user,
                    level_label=svc.controlled_level_label(c),
                    event="以搜尋內文比對命中",
                )
        data = ConversationOut.model_validate(c).model_dump()
        data["snippet"] = snippet
        hits.append(data)
    return hits


@router.get(
    "/{conv_id}",
    response_model=Union[ConversationDetail, ConversationTreeDetail],
)
def get_conversation(
    conv_id: int,
    tree: bool = Query(
        False,
        description=(
            "If true, return the full message tree (with parent_id) and "
            "active_leaf_message_id. Default returns the active path only, "
            "byte-compatible with pre-W2-3 clients."
        ),
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conv = svc.get_conversation(db, conv_id, current_user)
    # W1-1:原本這裡的條件是 legacy 的 classified boolean → 只有 >= 機密 才落
    # 稽核列,**營業秘密的
    # 讀取一列都沒有**。對營業秘密尤其致命:降密要兩個人加一份公文文號,而讀取
    # 連一列 log 都沒有 —— 不對稱到了荒謬的程度。
    if svc.is_controlled(conv):
        svc.log_controlled_access(
            db, conv_id, current_user, level_label=svc.controlled_level_label(conv)
        )
    messages = svc.list_messages_for_read(db, conv, tree=tree)
    base = ConversationOut.model_validate(conv).model_dump()
    if tree:
        return ConversationTreeDetail(
            **base,
            messages=[MessageTreeOut.model_validate(m) for m in messages],
            active_leaf_message_id=svc.resolve_active_leaf_id(db, conv),
        )
    return ConversationDetail(
        **base,
        messages=[MessageOut.model_validate(m) for m in messages],
    )


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
        parent_id=body.parent_id,
    )


@router.put("/{conv_id}/messages/{message_id}/rating", response_model=MessageOut)
def set_message_rating(
    conv_id: int,
    message_id: int,
    body: MessageRatingUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Record thumbs-up/down on an assistant message, or clear with rating=null.
    Optionally attaches structured feedback (comment + reason chips)."""
    return svc.set_message_rating(
        db, conv_id, message_id, current_user, body.rating,
        comment=body.comment, reasons=body.reasons,
    )


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
    """Grow a sibling user message (W2-3); old subtree is preserved.

    Returns the new sibling. The caller re-sends the chat turn so a fresh
    assistant child is appended under the new leaf.
    """
    return svc.edit_user_message(db, conv_id, message_id, current_user, body.content)


@router.post(
    "/{conv_id}/messages/{message_id}/fork",
    response_model=MessageOut,
    status_code=201,
)
def fork_assistant_message(
    conv_id: int,
    message_id: int,
    body: MessageUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Regenerate: create a sibling assistant under the same parent (C3 §b)."""
    if body.content is None:
        raise HTTPException(status_code=400, detail="content 必填")
    return svc.fork_assistant_message(
        db,
        conv_id,
        message_id,
        current_user,
        content=body.content,
        trace_id=body.trace_id,
        latency_ms=body.latency_ms,
        model_name=body.model_name,
        agent_name=body.agent_name,
        metadata=body.metadata,
    )


@router.put("/{conv_id}/active-leaf", response_model=ConversationOut)
def set_active_leaf(
    conv_id: int,
    body: ActiveLeafUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Point the active path at a message (version-switch persistence)."""
    return svc.set_active_leaf(db, conv_id, body.message_id, current_user)


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


# ── 匯出落列(W1-1④)──────────────────────────────────────────────────────────
#
# 缺口:前端的 `exportConversation` 原本**完全沒有分類 gate、沒有密等頁首、也不
# 落任何稽核列**。一份離開平台的檔案於是三件事都不成立 —— 收檔者不知道密等、沒人
# 知道是誰帶出去的、`export_records` 一列都沒有。
#
# 這個端點是「產檔前的收據」:前端**先落列成功才產檔**,失敗則擋(斷線 = 不放行)。
# 回應同時帶回**伺服器權威的密等頁首**,前端拿它蓋在檔案第一頁 —— 頁首不由前端
# 自己算,因為 client 手上的密等可能是舊的,而低報密等的檔案比不能匯出更糟。
#
# ⚠ 這是**紀錄面,不是授權面**。受控對話不回 403:它照樣落列(decision="deny")
# 並在回應裡告訴前端 `allowed=false`。理由是「有人試圖匯出營業秘密對話」正是稽核
# 最想看到的事件,而 403 會讓它連一列紀錄都沒有(對話匯出不經 policy engine,
# 沒有 PolicyDecision 可以接手記)。授權面在前端三個 gate 與未來的 policy engine。

# 台北 = UTC+8,無日光節約時間。與 `services/usage_service.py:_TPE_TZ` 同一個常數
# (呈現層一律 UTC+8,見補救計畫 D4);刻意不用 zoneinfo —— air-gapped 映像的
# tzdata 完整度不由本模組保證,而固定偏移對台灣是精確的。
_TAIPEI_TZ = timezone(timedelta(hours=8))

# 內網有多個平台,匯出檔不寫來源就查不到對應的稽核列。
_EXPORT_SOURCE_SYSTEM = "ANILA 平台（CSP）"

# 匯出目的地:對話匯出一律落到使用者的工作站(浮動檔案),不是某個受管空間。
_EXPORT_TARGET_SPACE = "local_download"


class ConversationExportRecordCreate(BaseModel):
    """匯出格式。白名單以外由 FastAPI 422 擋下(邊界 fail-closed)。"""

    format: str = Field(..., pattern="^(markdown|json)$")


class ConversationExportReceipt(BaseModel):
    """匯出收據。前端據此決定「產不產檔」與「頁首寫什麼」。"""

    # flag 狀態與實際落列結果分開回:required 是部署姿態,recorded 是這一次的事實。
    required: bool
    recorded: bool
    record_id: Optional[int] = None
    # 密等允許產檔嗎(> 無機密 → False,未知值 fail-closed → False)。
    allowed: bool
    classification_level: str
    exporter: str
    exported_at: str
    source_system: str
    export_format: str
    # 密等頁首,逐行 zh-TW。前端 markdown / json 兩種格式都蓋同一組字。
    header_lines: list[str]
    blocked_notice: Optional[str] = None


def _taipei_iso(value: datetime) -> str:
    """`YYYY-MM-DDTHH:MM:SS+08:00`。naive 值視同 UTC(csp 既有 convention)。"""
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_TAIPEI_TZ).isoformat(timespec="seconds")


# 密等不可讀時要寫進 `export_records.classification_level` 的值。
#
# 為什麼不能直接寫 `controlled_level_label()` 的「未知(視同受控)」:PostgreSQL 上
# 那個欄有 CHECK 約束(`ck_export_records_classification_level_gate2_level`,由
# r1_0011/r1_0017 建立)只允許五級字面值 —— 寫別的字會讓整個請求 500,於是**連
# 一列紀錄都沒有**。而測試用的 SQLite schema 由 `Base.metadata.create_all` 建立、
# 不含那個 CHECK,所以這種錯只會在生產爆掉(ledger 的 `sqlite-conftest-vs-pg`
# 記的正是這一類「測試綠但生產紅」)。
#
# 受限欄位上的 fail-closed = 取**最嚴等級**;真相(密等讀不出來)另記在
# `classification_source` 的 `:level_unreadable` 後綴,報表才分辨得出這一列是
# 資料損壞而不是真的絕對機密。使用者面/回應面仍顯示誠實的「未知(視同受控)」——
# 那是自由文字,沒有 CHECK。
_UNREADABLE_STORED_LEVEL = "絕對機密"


def _storable_classification_level(conv: Conversation) -> tuple[str, bool]:
    """回 `(可落庫的五級字面值, 密等是否讀得出來)`。"""
    try:
        return (
            ClassificationLevel.from_storage(conv.classification_level).to_storage(),
            True,
        )
    except (TypeError, ValueError):
        return _UNREADABLE_STORED_LEVEL, False


@router.post(
    "/{conv_id}/export-record",
    response_model=ConversationExportReceipt,
    status_code=200,
)
def record_conversation_export(
    conv_id: int,
    body: ConversationExportRecordCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conv = svc.get_conversation(db, conv_id, current_user)

    # 判定一律走 `conversation_service.is_controlled()` —— 五個外流面的單一判定
    # 來源(門檻「> 無機密」,未知值 fail-closed 視同受控)。**不要**在這裡自己
    # 讀 `conv.classified`:那個 boolean 的鏡射規則是 `classified = level >= 機密`,
    # 對營業秘密是 False,用它會讓紀錄自己說謊。
    controlled = svc.is_controlled(conv)
    level_label = svc.controlled_level_label(conv)
    now = datetime.now(timezone.utc)
    exported_at = _taipei_iso(now)
    header_lines = [
        f"密等：{level_label}",
        f"匯出者：{current_user.username}",
        f"匯出時間：{exported_at[:10]} {exported_at[11:19]}（UTC+8 / Asia/Taipei）",
        f"來源系統：{_EXPORT_SOURCE_SYSTEM}",
    ]

    blocked_notice = None
    if controlled:
        blocked_notice = (
            f"密等「{level_label}」的對話禁止匯出。依據：密等高於「無機密」時，"
            "複製／匯出／分享／列印四個外流面一律收緊並留下稽核紀錄；密等鎖定是"
            "單向的，使用者無法自行解除。替代路徑：在平台內繼續使用本對話，或走"
            "降密申請（需主管核准與公文文號）後由管理端匯出。"
        )

    stored_level, level_readable = _storable_classification_level(conv)

    record_id = None
    if settings.ANILA_EXPORT_RECORD_REQUIRED:
        record = ExportRecord(
            # 對話匯出沒有 artifact —— 這正是 r1_0038 把 artifact_id 放寬的理由。
            artifact_id=None,
            conversation_id=conv.id,
            exporter_user_id=current_user.id,
            target_space=_EXPORT_TARGET_SPACE,
            export_format=body.format,
            # deny 也落列:對話匯出不經 policy engine,不落這裡就完全沒有紀錄。
            decision="deny" if controlled else "allow",
            classification_level=stored_level,
            classification_source=(
                "conversation_export" if level_readable
                else "conversation_export:level_unreadable"
            ),
            # created_at 交給模型的 `_utcnow` 預設 —— 不在這裡另寫一份時間來源。
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        record_id = record.id

    return ConversationExportReceipt(
        required=settings.ANILA_EXPORT_RECORD_REQUIRED,
        recorded=record_id is not None,
        record_id=record_id,
        allowed=not controlled,
        classification_level=level_label,
        exporter=current_user.username,
        exported_at=exported_at,
        source_system=_EXPORT_SOURCE_SYSTEM,
        export_format=body.format,
        header_lines=header_lines,
        blocked_notice=blocked_notice,
    )
