"""Handoff and notification endpoints."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.database import get_db
from app.models.user import User
from app.services import handoff_service as svc

router = APIRouter(tags=["handoffs"])

# Colleague-handoff UI was removed pending OWNER Q9. Accept/reject only flipped
# status + notification and never transferred conversation ownership — keeping
# that silent success is worse than refusing. Agent handoff in the shell is a
# client-side agent switch and does not use these endpoints.
_HANDOFF_RESOLVE_NOT_IMPLEMENTED = (
    "對話交接的接受/拒絕尚未實作所有權移交;"
    "目前只會改狀態卻不把對話交給對方。"
    "同事交接待擁有者決定查詢範圍(OWNER Q9);"
    "交給其他助手請用聊天介面的助手選單。"
)


# ── Schemas ───────────────────────────────────────────────────────────────────

class HandoffCreate(BaseModel):
    conversation_id: int
    to_user_id: Optional[int] = None
    to_agent: Optional[str] = None
    note: Optional[str] = None


class HandoffOut(BaseModel):
    id: int
    conversation_id: int
    from_user_id: Optional[int]
    to_user_id: Optional[int]
    to_agent: Optional[str]
    status: str
    note: Optional[str]
    resolved_at: Optional[datetime]
    created_at: datetime
    model_config = {"from_attributes": True}


class NotificationOut(BaseModel):
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
    return svc.list_my_handoffs(db, current_user)


@router.post("/api/handoffs", response_model=HandoffOut, status_code=201)
def create_handoff(
    body: HandoffCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
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
    raise HTTPException(status_code=501, detail=_HANDOFF_RESOLVE_NOT_IMPLEMENTED)


@router.post("/api/handoffs/{handoff_id}/reject", response_model=HandoffOut)
def reject_handoff(
    handoff_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    raise HTTPException(status_code=501, detail=_HANDOFF_RESOLVE_NOT_IMPLEMENTED)


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
