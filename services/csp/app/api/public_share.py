"""Public read-only share page — no authentication required."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
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
    if not settings.ENABLE_PUBLIC_SHARE:
        # Air-gapped / card-only deployments disable the unauthenticated share
        # surface entirely. 404 (not 403) so its existence can't be probed.
        raise HTTPException(status_code=404, detail="Not Found")

    share = get_share_by_token(db, token)
    conv = share.conversation

    # OE-4 two-line gate (SYSTEM-MAP §8 L241): redact content/title when
    # level >= RESTRICTED (密). Boolean is display-only — do not decide here.
    from app.schemas.contracts.classification import (
        ClassificationLevel,
        outbound_action_allowed,
    )
    level = ClassificationLevel.from_storage(conv.classification_level)
    outbound_ok = outbound_action_allowed(level)
    messages: list[PublicMessageOut] = []
    if outbound_ok:
        for msg in conv.messages:
            messages.append(PublicMessageOut.model_validate(msg))

    # Title redaction for outbound-blocked conversations. Use 列管 rather
    # than naming a specific level (SYSTEM-MAP §8 L268 honest labeling).
    return PublicShareOut(
        share_token=token,
        conversation_id=conv.id,
        conversation_title="（列管對話）" if not outbound_ok else conv.title,
        mode=share.mode,
        allow_fork=share.allow_fork,
        expires_at=share.expires_at,
        view_count=share.view_count,
        messages=messages,
    )
