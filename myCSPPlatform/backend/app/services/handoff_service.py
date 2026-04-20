"""Handoff and notification persistence service."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.handoff import Handoff, Notification
from app.models.user import User


# ── Handoffs ──────────────────────────────────────────────────────────────────

def create_handoff(
    db: Session,
    conversation_id: int,
    from_user: User,
    *,
    to_user_id: Optional[int] = None,
    to_agent: Optional[str] = None,
    note: Optional[str] = None,
) -> Handoff:
    if not to_user_id and not to_agent:
        raise HTTPException(status_code=400, detail="必須指定接收對象 (to_user_id 或 to_agent)")

    handoff = Handoff(
        conversation_id=conversation_id,
        from_user_id=from_user.id,
        to_user_id=to_user_id,
        to_agent=to_agent,
        note=note,
        status="pending",
    )
    db.add(handoff)
    db.flush()

    # Create notification for the recipient user
    if to_user_id:
        _create_notification(
            db,
            user_id=to_user_id,
            type="handoff_request",
            title="收到對話交接請求",
            body=f"{from_user.username} 想將對話轉交給您。{(' 備註：' + note) if note else ''}",
            payload={
                "handoff_id": handoff.id,
                "conversation_id": conversation_id,
                "from_user": from_user.username,
            },
        )

    db.commit()
    db.refresh(handoff)
    return handoff


def resolve_handoff(
    db: Session,
    handoff_id: int,
    user: User,
    *,
    accept: bool,
) -> Handoff:
    handoff = db.query(Handoff).filter(Handoff.id == handoff_id).first()
    if not handoff:
        raise HTTPException(status_code=404, detail="找不到此交接請求")
    if handoff.to_user_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="無權處理此交接請求")
    if handoff.status != "pending":
        raise HTTPException(status_code=409, detail=f"此交接請求已處理 (狀態: {handoff.status})")

    handoff.status = "accepted" if accept else "rejected"
    handoff.resolved_at = datetime.now(timezone.utc)
    handoff.updated_at = datetime.now(timezone.utc)

    # Notify originator
    if handoff.from_user_id:
        action = "接受" if accept else "拒絕"
        _create_notification(
            db,
            user_id=handoff.from_user_id,
            type="handoff_accepted" if accept else "handoff_rejected",
            title=f"交接請求已{action}",
            body=f"{user.username} 已{action}您的對話交接請求。",
            payload={
                "handoff_id": handoff.id,
                "conversation_id": handoff.conversation_id,
                "resolved_by": user.username,
            },
        )

    db.commit()
    db.refresh(handoff)
    return handoff


def cancel_handoff(db: Session, handoff_id: int, user: User) -> Handoff:
    handoff = db.query(Handoff).filter(Handoff.id == handoff_id).first()
    if not handoff:
        raise HTTPException(status_code=404, detail="找不到此交接請求")
    if handoff.from_user_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="無權取消此交接請求")
    if handoff.status != "pending":
        raise HTTPException(status_code=409, detail="只能取消 pending 狀態的交接請求")
    handoff.status = "cancelled"
    handoff.resolved_at = datetime.now(timezone.utc)
    handoff.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(handoff)
    return handoff


def list_my_handoffs(db: Session, user: User) -> list[Handoff]:
    """Return handoffs where user is sender OR receiver."""
    return (
        db.query(Handoff)
        .filter(
            (Handoff.from_user_id == user.id) | (Handoff.to_user_id == user.id)
        )
        .order_by(Handoff.created_at.desc())
        .all()
    )


# ── Notifications ─────────────────────────────────────────────────────────────

def get_notifications(
    db: Session,
    user: User,
    *,
    unread_only: bool = False,
    limit: int = 50,
) -> list[Notification]:
    q = db.query(Notification).filter(Notification.user_id == user.id)
    if unread_only:
        q = q.filter(Notification.is_read == False)
    return q.order_by(Notification.created_at.desc()).limit(limit).all()


def mark_notification_read(db: Session, notif_id: int, user: User) -> Notification:
    notif = db.query(Notification).filter(
        Notification.id == notif_id,
        Notification.user_id == user.id,
    ).first()
    if not notif:
        raise HTTPException(status_code=404, detail="找不到此通知")
    notif.is_read = True
    db.commit()
    db.refresh(notif)
    return notif


def mark_all_read(db: Session, user: User) -> int:
    count = (
        db.query(Notification)
        .filter(Notification.user_id == user.id, Notification.is_read == False)
        .update({"is_read": True})
    )
    db.commit()
    return count


# ── Internal ──────────────────────────────────────────────────────────────────

def _create_notification(
    db: Session,
    *,
    user_id: int,
    type: str,
    title: str,
    body: str,
    payload: Optional[dict] = None,
) -> Notification:
    notif = Notification(
        user_id=user_id,
        type=type,
        title=title,
        body=body,
        payload=payload,
    )
    db.add(notif)
    return notif
