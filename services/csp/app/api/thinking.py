"""Cookie-auth thinking-summary endpoint.

殼層不再呼叫這支。非特權呼叫者仍回 200，但 summary 為 null，也不打模型。
擁有者、管理員、開發者一般失敗時同樣回 200、summary 為 null；額度擋下要原樣回 429 與重置說明。
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.services.auth_service import get_current_user
from app.services.internal_llm import InternalCompletionError
from app.services.reasoning_gate import may_see_raw_reasoning
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
    if not may_see_raw_reasoning(_user):
        return ThinkingSummaryOut(summary=None)
    added = body.added or ""
    if len(added) > 16_384:
        added = added[-16_384:]
    previous = [
        {"text": str(item.get("text") or "")}
        for item in (body.previous or [])[:MAX_HISTORY]
        if isinstance(item, dict)
    ]
    try:
        text = await summarize_reasoning_batch(
            db,
            added=added,
            previous=previous,
            user_id=_user.id,
            department_id=getattr(_user, "department_id", None),
        )
    except InternalCompletionError as exc:
        if exc.status_code == 429 and exc.quota_code == "quota_exceeded" and exc.quota_message:
            raise HTTPException(
                status_code=429,
                detail={"code": "quota_exceeded", "message": exc.quota_message},
            ) from exc
        text = None
    except Exception:
        text = None
    return ThinkingSummaryOut(summary=text)
