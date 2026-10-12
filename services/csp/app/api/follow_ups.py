"""POST /api/conversations/{cid}/messages/{mid}/follow-ups。

掛在既有的 conversations router 上，所以 CSRF 與其他 POST 同一套。
只有對話擁有者能呼叫；別人（含被分享的人與非擁有者的管理員）得到 404。
"""
from __future__ import annotations

from fastapi import Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.api.conversations import router as conversations_router
from app.database import get_db
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User
from app.services.follow_ups_service import suggest_follow_ups


class FollowUpsOut(BaseModel):
    suggestions: list[str]


@conversations_router.post(
    "/{conv_id}/messages/{message_id}/follow-ups",
    response_model=FollowUpsOut,
)
async def create_message_follow_ups(
    conv_id: int,
    message_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conv = db.get(Conversation, conv_id)
    if conv is None or conv.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="對話不存在")
    msg = db.get(Message, message_id)
    if msg is None or msg.conversation_id != conv.id:
        raise HTTPException(status_code=404, detail="訊息不存在")
    suggestions = await suggest_follow_ups(db, current_user, conv, msg)
    return FollowUpsOut(suggestions=suggestions)
