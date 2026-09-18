"""Cookie-auth thinking-summary endpoint. Fail-open: always 200."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.services.auth_service import get_current_user
from app.services.thinking_summary import MAX_HISTORY, summarize_reasoning_batch

router = APIRouter(prefix="/api/thinking", tags=["thinking"])


class ThinkingSummaryIn(BaseModel):
    added: str = ""
    previous: list[dict[str, Any]] = Field(default_factory=list)


class ThinkingSummaryOut(BaseModel):
    summary: Optional[str] = None


@router.post("/summarize", response_model=ThinkingSummaryOut)
async def summarize_thinking(
    body: ThinkingSummaryIn,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> ThinkingSummaryOut:
    added = body.added or ""
    if len(added) > 16_384:
        added = added[-16_384:]
    previous = [
        {"text": str(item.get("text") or "")}
        for item in (body.previous or [])[:MAX_HISTORY]
        if isinstance(item, dict)
    ]
    try:
        text = await summarize_reasoning_batch(db, added=added, previous=previous)
    except Exception:
        text = None
    return ThinkingSummaryOut(summary=text)
