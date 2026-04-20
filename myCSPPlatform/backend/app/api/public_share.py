"""Public read-only share page — no authentication required."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.conversation_service import get_share_by_token

router = APIRouter(prefix="/api/public/share", tags=["public-share"])


class PublicMessageOut(BaseModel):
    id: int
    role: str
    content: str
    trace_id: Optional[str]
    latency_ms: Optional[int]
    model_name: Optional[str]
    agent_name: Optional[str]
    created_at: datetime
    model_config = {"from_attributes": True}


class PublicShareOut(BaseModel):
    share_token: str
    conversation_id: int
    conversation_title: str
    mode: str
    allow_fork: bool
    expires_at: Optional[datetime]
    view_count: int
    messages: list[PublicMessageOut]


@router.get("/{token}", response_model=PublicShareOut)
def get_shared_conversation(
    token: str,
    db: Session = Depends(get_db),
):
    """Return a read-only view of a shared conversation. No auth required."""
    share = get_share_by_token(db, token)
    conv = share.conversation

    messages: list[PublicMessageOut] = []
    for msg in conv.messages:
        # Never expose classified conversation content publicly (belt + suspenders)
        if conv.classified:
            break
        messages.append(PublicMessageOut.model_validate(msg))

    return PublicShareOut(
        share_token=token,
        conversation_id=conv.id,
        conversation_title=conv.title,
        mode=share.mode,
        allow_fork=share.allow_fork,
        expires_at=share.expires_at,
        view_count=share.view_count,
        messages=messages,
    )
