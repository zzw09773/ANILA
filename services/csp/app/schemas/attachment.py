"""Attachment API / response schemas (P1.5 extraction + capacity)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field
from app.schemas.base import ApiResponseModel


class ConversationCapacity(BaseModel):
    """Per-conversation attachment token budget snapshot for the client meter.

    Invariant: ``used_tokens + remaining_tokens == budget_tokens``.
    ``used_tokens`` is the sum of admitted effective costs (clamped to budget);
    ``over_budget_tokens`` is the sum of ok-but-excluded effective costs so a
    client can render overflow honestly without breaking the bar math.

    Admission is derived for the request's model (``?model=`` or default);
    it is never persisted on extract_status.
    """

    used_tokens: int
    budget_tokens: int
    remaining_tokens: int
    over_budget_tokens: int = 0
    percent: int = Field(ge=0, le=100)
    attachment_count: int


class AttachmentOut(ApiResponseModel):
    reference_id: str
    filename: str
    content_type: str
    size_bytes: int
    conversation_id: Optional[int] = None
    message_id: Optional[int] = None
    created_at: datetime
    # 抽取時取得,供 UI 顯示「500 頁」之類的規模提示。
    page_count: Optional[int] = None
    # Extraction outcome only: pending | ok | failed | unsupported | too_large
    extract_status: str = "pending"
    # Derived for this request's model budget; independent of extract_status.
    budget_admitted: bool = False
    token_count: Optional[int] = None
    extract_error: Optional[str] = None
    conversation_capacity: Optional[ConversationCapacity] = None
    model_config = {"from_attributes": True}


class ConversationAttachmentsOut(BaseModel):
    attachments: list[AttachmentOut]
    conversation_capacity: ConversationCapacity
