"""使用者回饋總覽(P3.4)—— ``GET /api/admin/feedback``。

這頁是給**單一維運者早上掃一輪**用的,不是對話瀏覽器。

用途
----
預設看近 7 天的差評(``rating=down``)與有留言的回饋,可再依時間、agent、
model 篩。維運者據此決定要不要調 prompt、換模型,或拿 ``conversation_id``
走正式的對話讀取路徑追查。一牆沒人讀的訊息全文不是目標。

密等規則(本包唯一紅線)
----------------------
訊息正文(``Message.content``)是受控對話內容。既有讀取路徑是
``GET /api/conversations/{id}``:admin-tier 可讀,且密等 ≥ 營業秘密時會落
``access_classified_conversation`` 稽核;單位管理員明確**看不到對話明文**
(PLAN P1.3)。

因此本列表端點:

1. ``require_admin``(admin + owner)——非 admin-tier(含單位管理員、一般
   使用者、developer)一律 403。
2. **回應白名單永不含 ``content``** —— 即使呼叫者是 admin,這支列表也不
   得變成受控對話的旁路批量讀取面。需要正文時走已稽核的對話 GET。
3. 回饋留言(``metadata.feedback.comment`` / ``reasons``)是使用者主動留下
   的品質訊號,屬於本頁要看的東西;密等等級以徽章標出供判斷。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User
from app.services.auth_service import require_admin
from app.schemas.base import ApiResponseModel

router = APIRouter(prefix="/api/admin/feedback", tags=["使用者回饋"])

#: 回應允許的欄位。任何新增欄位都要先過釘住它的測試 —— 尤其不得加 content。
FEEDBACK_ITEM_KEYS = frozenset(
    {
        "message_id",
        "conversation_id",
        "rating",
        "comment",
        "reasons",
        "model_name",
        "agent_name",
        "classification_level",
        "message_created_at",
        "username",
    }
)


class FeedbackItem(ApiResponseModel):
    """單一評分列。**這個 model 就是白名單** —— 不要加 ``content``。"""

    message_id: int
    conversation_id: int
    rating: str
    comment: str | None = None
    reasons: list[str] = Field(default_factory=list)
    model_name: str | None = None
    agent_name: str | None = None
    classification_level: str
    message_created_at: datetime
    username: str | None = None


class FeedbackSummary(BaseModel):
    total: int
    up: int
    down: int
    with_comment: int


class FeedbackListResponse(BaseModel):
    summary: FeedbackSummary
    items: list[FeedbackItem]


def _feedback_from_metadata(meta: dict | None) -> tuple[str | None, list[str]]:
    if not isinstance(meta, dict):
        return None, []
    raw = meta.get("feedback")
    if not isinstance(raw, dict):
        return None, []
    comment = raw.get("comment")
    if comment is not None:
        comment = str(comment).strip() or None
    reasons_raw = raw.get("reasons") or []
    reasons = [str(r) for r in reasons_raw if r] if isinstance(reasons_raw, list) else []
    return comment, reasons


def _item_from_row(
    msg: Message, conv: Conversation, username: str | None
) -> FeedbackItem:
    comment, reasons = _feedback_from_metadata(msg.metadata_)
    # Conversation-level classification is the latch the platform enforces;
    # message-level may lag on older rows — take the higher of the two by
    # storage string via from_storage when both present.
    from app.schemas.contracts.classification import ClassificationLevel

    levels = []
    for raw in (conv.classification_level, msg.classification_level):
        if raw:
            try:
                levels.append(ClassificationLevel.from_storage(raw))
            except ValueError:
                continue
    level = (
        ClassificationLevel.max_of(levels).to_storage()
        if levels
        else "無機密"
    )
    return FeedbackItem(
        message_id=msg.id,
        conversation_id=conv.id,
        rating=msg.rating or "",
        comment=comment,
        reasons=reasons,
        model_name=msg.model_name,
        agent_name=msg.agent_name,
        classification_level=level,
        message_created_at=msg.created_at,
        username=username,
    )


@router.get("", response_model=FeedbackListResponse)
def list_feedback(
    rating: Literal["up", "down"] | None = Query(
        None, description="只看讚或爛;預設全部有評分的"
    ),
    agent_name: str | None = Query(None, max_length=100),
    model_name: str | None = Query(None, max_length=100),
    days: int = Query(
        7,
        ge=1,
        le=90,
        description="回看幾天(依訊息 created_at;評分沒有獨立時間戳)",
    ),
    only_with_comment: bool = Query(
        False, description="只看有文字留言的(差評追查常用)"
    ),
    limit: int = Query(100, ge=1, le=500),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> FeedbackListResponse:
    """列出有評分的助理訊息。永不回傳訊息正文。"""
    since = datetime.now(timezone.utc) - timedelta(days=days)

    q = (
        db.query(Message, Conversation, User.username)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .outerjoin(User, User.id == Conversation.user_id)
        .filter(Message.rating.isnot(None))
        .filter(Message.created_at >= since)
        .order_by(Message.created_at.desc())
    )
    if rating:
        q = q.filter(Message.rating == rating)
    if agent_name:
        q = q.filter(Message.agent_name == agent_name)
    if model_name:
        q = q.filter(Message.model_name == model_name)

    # Pull a bit more than limit when filtering comments in Python — feedback
    # lives in JSON metadata and SQLite/Postgres JSON path differs; keep the
    # filter correct over the bounded window instead of dialect-specific SQL.
    fetch_cap = min(max(limit * 5, limit), 2000) if only_with_comment else limit
    rows = q.limit(fetch_cap).all()

    items: list[FeedbackItem] = []
    for msg, conv, username in rows:
        item = _item_from_row(msg, conv, username)
        if only_with_comment and not (item.comment or item.reasons):
            continue
        items.append(item)
        if len(items) >= limit:
            break

    summary = FeedbackSummary(
        total=len(items),
        up=sum(1 for i in items if i.rating == "up"),
        down=sum(1 for i in items if i.rating == "down"),
        with_comment=sum(1 for i in items if i.comment or i.reasons),
    )
    return FeedbackListResponse(summary=summary, items=items)
