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

匯出(``?format=csv``)
---------------------
維運者要能把回饋拉出來排序、統計,不是只能捲畫面。CSV 走**同一支端點、
同一個 ``require_admin``、同一份白名單** —— 匯出不是另一條讀取路徑,所以
不會出現「JSON 擋住、CSV 漏出」的分歧。列數上限見 ``FEEDBACK_EXPORT_MAX_ROWS``。
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
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
        "rating_score",
        "comment",
        "reasons",
        "model_name",
        "agent_name",
        "classification_level",
        "message_created_at",
        "username",
    }
)

#: CSV 匯出的列數天花板。超過就**明講並拒絕**,不悄悄少給 —— 檔案下載後
#: HTTP header 就消失了,被截斷的 CSV 在 Excel 裡完全沒有痕跡,那正是本專案
#: 這週在抓的「看起來成功、實際少做」缺陷。5000 列 ≈ 1 MB,Excel 秒開;
#: 真的更多時,縮天數或加 agent／模型篩選才是維運者想要的動作。
FEEDBACK_EXPORT_MAX_ROWS = 5000

#: 台北 = UTC+8。CSV 直接餵 Excel,沒有前端 JS 可以做時區轉換,所以在這層轉
#: (與 ``usage_service._to_tpe_iso`` 同 convention:naive 一律當 UTC)。
_TPE_TZ = timezone(timedelta(hours=8))

#: CSV 欄位順序與繁中表頭;key 一律取自 :data:`FEEDBACK_ITEM_KEYS` 白名單,
#: 順序對齊畫面欄位。
_CSV_COLUMNS: tuple[tuple[str, str], ...] = (
    ("rating", "評分"),
    # 兩個五分尺,不是一條十分尺 —— 表頭寫清楚,免得 Excel 裡被當成絕對分。
    ("rating_score", "分數(讚6-10／爛1-5)"),
    ("comment", "留言"),
    ("reasons", "原因"),
    ("agent_name", "Agent"),
    ("model_name", "模型"),
    ("classification_level", "密等"),
    ("message_created_at", "訊息時間"),
    ("username", "使用者"),
    ("conversation_id", "對話 ID"),
    ("message_id", "訊息 ID"),
)

_RATING_LABELS = {"down": "爛", "up": "讚"}


class FeedbackItem(ApiResponseModel):
    """單一評分列。**這個 model 就是白名單** —— 不要加 ``content``。"""

    message_id: int
    conversation_id: int
    rating: str
    # None = 舊列或只按拇指沒選數字;有值時與 rating 配對(讚 6–10／爛 1–5)。
    rating_score: int | None = None
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
        rating_score=msg.rating_score,
        comment=comment,
        reasons=reasons,
        model_name=msg.model_name,
        agent_name=msg.agent_name,
        classification_level=level,
        message_created_at=msg.created_at,
        username=username,
    )


def _csv_value(key: str, value) -> str:
    if key == "reasons":
        return " · ".join(value or [])
    if key == "rating":
        return _RATING_LABELS.get(value, value or "")
    if key == "rating_score":
        return "" if value is None else str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(_TPE_TZ).isoformat()
    return "" if value is None else str(value)


def _items_to_csv(items: list[FeedbackItem]) -> str:
    """展平成 CSV;首列 BOM 供 Excel 正確以 UTF-8 開啟。

    欄位只從 :class:`FeedbackItem`(= 白名單)取。白名單裡若出現 ``_CSV_COLUMNS``
    沒宣告的鍵,會以原鍵名補在最後一欄 —— 這是刻意的絆線:任何人放寬白名單,
    這裡會立刻長出一欄而被測試打紅,而不是讓 CSV 與 JSON 悄悄分歧。
    """
    declared = {key for key, _ in _CSV_COLUMNS}
    extra = [key for key in sorted(FEEDBACK_ITEM_KEYS) if key not in declared]
    columns = [*_CSV_COLUMNS, *((key, key) for key in extra)]

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([header for _, header in columns])
    for item in items:
        data = item.model_dump()
        writer.writerow([_csv_value(key, data.get(key)) for key, _ in columns])
    return "﻿" + buffer.getvalue()


def _export_csv(q, *, only_with_comment: bool) -> StreamingResponse:
    """把**整個篩選結果**(不是畫面當前那頁)展成 CSV。

    ``only_with_comment`` 是 Python 端篩選(留言在 JSON metadata 裡,SQLite 與
    Postgres 的 JSON path 語法不同),所以這裡不能靠 SQL ``LIMIT`` 湊列數 ——
    掃到的列被留言篩掉時,那會給出一份比實際少的檔案。改成 ``yield_per`` 逐批
    掃,湊滿上限就停,而超過上限一律回 400 說清楚。
    """
    items: list[FeedbackItem] = []
    overflow = False
    for msg, conv, username in q.yield_per(500):
        item = _item_from_row(msg, conv, username)
        if only_with_comment and not (item.comment or item.reasons):
            continue
        if len(items) >= FEEDBACK_EXPORT_MAX_ROWS:
            overflow = True
            break
        items.append(item)

    if overflow:
        raise HTTPException(
            status_code=400,
            detail=(
                f"符合目前篩選的回饋超過匯出上限 {FEEDBACK_EXPORT_MAX_ROWS} 列,"
                "因此沒有匯出。請縮短天數,或加上評分／agent／模型篩選後再匯出 ——"
                "寧可明講不給,也不給一份被默默砍短的檔案。"
            ),
        )

    filename = f"feedback-{datetime.now(_TPE_TZ).strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([_items_to_csv(items)]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "X-Feedback-Export-Rows": str(len(items)),
        },
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
    format: Literal["json", "csv"] = Query(
        "json", description="csv = 依目前篩選匯出(忽略 limit,匯出整個篩選結果)"
    ),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> FeedbackListResponse | StreamingResponse:
    """列出有評分的助理訊息。永不回傳訊息正文。

    ``?format=csv`` 用**同樣的篩選條件**回傳含 BOM 的 UTF-8 ``text/csv``,
    且不受 ``limit`` 這個畫面分頁參數限制(匯出的是整個篩選結果)。筆數超過
    ``FEEDBACK_EXPORT_MAX_ROWS`` 時回 400 講清楚,不會給一份被默默砍短的檔案。
    """
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

    if format == "csv":
        return _export_csv(q, only_with_comment=only_with_comment)

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
