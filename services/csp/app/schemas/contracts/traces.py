# -*- coding: utf-8 -*-
"""TraceSpan 契約(doc 01 §7 / doc 05 §6 / doc 09 span event schema,
Slice 2a)。

``TraceSpanIn`` 是 Slice 4 的 ``POST /v1/traces/{trace_id}/spans`` 將接受
的單一 span payload,欄位對齊 doc 09:span_id / parent_span_id / trace_id /
span_type / name / status / started_at / ended_at / attributes。
``trace_id`` 可省略 —— ingest 端以 URL path 的 trace_id 為準,body 內
給了就必須一致(Slice 4 驗證)。``producer`` / ``task_id`` 由 ingest 端
依呼叫者身分設定,不收 client 自報。

``span_type`` 維持開放字串:doc 05 §6 的 13 個必備 agent span type 列於
:data:`REQUIRED_AGENT_SPAN_TYPES`,封閉 enum 收斂保留給 Slice 4。
"""

from __future__ import annotations

from app.schemas.base import ApiResponseModel

import enum
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from anila_contracts import Classification as ClassificationLevel

# doc 05 §6 — 一次正式 agent run 必須產出的 13 個 span type(逐字)。
REQUIRED_AGENT_SPAN_TYPES: tuple[str, ...] = (
    "agent.run.started",
    "agent.step.started",
    "agent.step.finished",
    "agent.model_call.started",
    "agent.model_call.finished",
    "agent.tool_call.started",
    "agent.tool_call.finished",
    "agent.retrieval.started",
    "agent.retrieval.finished",
    "agent.output.started",
    "agent.output.finished",
    "agent.error",
    "agent.run.finished",
)


class SpanStatus(str, enum.Enum):
    """doc 01 TraceSpan.status 三值。"""

    OK = "ok"
    ERROR = "error"
    CANCELLED = "cancelled"


class SpanProducer(str, enum.Enum):
    """span 來源腳色五值(trace 貫穿 task/proxy/router/agent/model/studio,
    doc 02)。"""

    ROUTER = "router"
    PROXY = "proxy"
    AGENT = "agent"
    MODEL = "model"
    STUDIO = "studio"


class TraceSpanIn(BaseModel):
    """單一 span 的 ingest 契約(doc 09 span event schema)。"""

    span_id: str = Field(min_length=1, max_length=64)
    parent_span_id: str | None = None
    # 可省略;Slice 4 以 path trace_id 為準,body 給值必須一致。
    trace_id: str | None = None
    # 開放字串;必備 13 型別見 REQUIRED_AGENT_SPAN_TYPES(Slice 4 收斂)。
    span_type: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    status: SpanStatus = SpanStatus.OK
    started_at: datetime | None = None
    ended_at: datetime | None = None
    attributes: dict[str, Any] | None = None
    # Producer may only raise the task-derived floor.  The ingest service
    # computes max(task, supplied); omission never means 無機密.
    classification_level: ClassificationLevel | None = None


class TraceSpanOut(ApiResponseModel):
    """TraceSpan 讀出契約(from ORM)。"""

    id: int
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    task_id: int | None = None
    span_type: str
    name: str
    status: SpanStatus
    started_at: datetime | None = None
    ended_at: datetime | None = None
    attributes: dict[str, Any] | None = None
    producer: SpanProducer
    classification_level: ClassificationLevel
    created_at: datetime

    model_config = {"from_attributes": True}
