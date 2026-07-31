"""Handoff and notification endpoints."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.database import get_db
from app.models.user import User
from app.services import handoff_service as svc
from app.services import handoff_transfer as transfer
from app.schemas.base import ApiResponseModel

router = APIRouter(tags=["handoffs"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class HandoffCreate(BaseModel):
    conversation_id: int
    to_user_id: Optional[int] = None
    to_agent: Optional[str] = None
    note: Optional[str] = None


class HandoffOut(ApiResponseModel):
    id: int
    conversation_id: int
    from_user_id: Optional[int]
    to_user_id: Optional[int]
    to_agent: Optional[str]
    status: str
    note: Optional[str]
    resolved_at: Optional[datetime]
    created_at: datetime
    # 收件匣要看得懂才按得下去 ——「交接 #7」對使用者沒有意義。兩個欄位都是
    # 唯讀顯示用,只在列表端點填;對方帳號與對話標題本來就是送出者指名要
    # 讓收件人看到的東西。
    from_username: Optional[str] = None
    conversation_title: Optional[str] = None
    model_config = {"from_attributes": True}


class NotificationOut(ApiResponseModel):
    id: int
    type: str
    title: str
    body: str
    payload: Optional[Any]
    is_read: bool
    created_at: datetime
    model_config = {"from_attributes": True}


class MarkReadBulk(BaseModel):
    ids: list[int]


# ── Handoff endpoints ─────────────────────────────────────────────────────────

@router.get("/api/handoffs", response_model=list[HandoffOut])
def list_handoffs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = svc.list_my_handoffs(db, current_user)
    return [_handoff_out(db, row) for row in rows]


def _handoff_out(db: Session, handoff) -> HandoffOut:
    """補上顯示用的送出者帳號與對話標題(缺就留 None,不讓列表 500)。"""
    from app.models.conversation import Conversation

    out = HandoffOut.model_validate(handoff)
    if handoff.from_user is not None:
        out.from_username = handoff.from_user.username
    conv = (
        db.query(Conversation.title)
        .filter(Conversation.id == handoff.conversation_id)
        .first()
    )
    if conv is not None:
        out.conversation_title = conv[0]
    return out


@router.post("/api/handoffs", response_model=HandoffOut, status_code=201)
def create_handoff(
    body: HandoffCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Same write gate as every other conversation mutation
    # (get_conversation for_write=True): owner or admin. Share recipients
    # must not hand off someone else's thread.
    from app.services.conversation_service import get_conversation

    conv = get_conversation(db, body.conversation_id, current_user, for_write=True)
    if body.to_user_id is not None:
        # 只有「交給人」才是外流動作。交給 agent 是同一串對話換助手,
        # 不會離開這個帳號,不套外流上限(不憑空加限制)。
        transfer.guard_transferable(conv)
        transfer.resolve_new_owner(db, body.to_user_id, actor=current_user)
    return svc.create_handoff(
        db, body.conversation_id, current_user,
        to_user_id=body.to_user_id,
        to_agent=body.to_agent,
        note=body.note,
    )


@router.post("/api/handoffs/{handoff_id}/accept", response_model=HandoffOut)
def accept_handoff(
    handoff_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """接受交接 —— 對話真的換手(見 handoff_transfer 模組 docstring)。"""
    return transfer.accept_handoff(db, handoff_id, current_user)


@router.post("/api/handoffs/{handoff_id}/reject", response_model=HandoffOut)
def reject_handoff(
    handoff_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """拒絕交接 —— 對話一動也不動,只翻狀態並通知送出的人。"""
    return transfer.reject_handoff(db, handoff_id, current_user)


@router.post("/api/handoffs/{handoff_id}/cancel", response_model=HandoffOut)
def cancel_handoff(
    handoff_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return svc.cancel_handoff(db, handoff_id, current_user)


# ── Notification endpoints ────────────────────────────────────────────────────

@router.get("/api/notifications", response_model=list[NotificationOut])
def list_notifications(
    unread_only: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return svc.get_notifications(db, current_user, unread_only=unread_only, limit=limit)


@router.put("/api/notifications/{notif_id}/read", response_model=NotificationOut)
def mark_read(
    notif_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return svc.mark_notification_read(db, notif_id, current_user)


@router.put("/api/notifications/read-all")
def mark_all_read(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    count = svc.mark_all_read(db, current_user)
    return {"marked_read": count}
