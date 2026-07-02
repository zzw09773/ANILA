# -*- coding: utf-8 -*-
"""TraceSpan — Full Trace Protocol 的持久化 span(doc 01 §7 / doc 05 §6,
Slice 2a)。

span tree 由 (trace_id, span_id, parent_span_id) 重建;(trace_id, span_id)
組合唯一(冪等 ingest 的基礎)。

``span_type`` 是**開放 String**:doc 05 §6 要求一次正式 agent run 產出
13 個必備 span type ——

    agent.run.started / agent.step.started / agent.step.finished /
    agent.model_call.started / agent.model_call.finished /
    agent.tool_call.started / agent.tool_call.finished /
    agent.retrieval.started / agent.retrieval.finished /
    agent.output.started / agent.output.finished /
    agent.error / agent.run.finished

—— 但封閉 enum 的收斂(含 doc 01 的 task/policy/... 十值語彙)保留給
Slice 4(Full Trace ingestion 落地時)再定;現階段常數清單見
``app.schemas.contracts.traces.REQUIRED_AGENT_SPAN_TYPES``。

``producer`` 記 span 來源腳色(router/proxy/agent/model/studio),
``status`` 三值 ok/error/cancelled(doc 01)—— 皆存 String、Pydantic
契約層把關(SQLite create_all 相容,不用 PG 原生 enum)。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base

JSONValue = JSON().with_variant(JSONB, "postgresql")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TraceSpan(Base):
    """一個 trace 內的單一 span;(trace_id, span_id) 唯一。"""

    __tablename__ = "trace_spans"
    __table_args__ = (
        Index("uq_trace_spans_trace_span", "trace_id", "span_id",
              unique=True),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    trace_id = Column(String(64), nullable=False, index=True)
    span_id = Column(String(64), nullable=False)
    parent_span_id = Column(String(64), nullable=True)
    # 可空:非正式任務(legacy runtime call)也可能產 trace。
    # SET NULL —— 刪 task 保留觀測史。
    task_id = Column(
        Integer, ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    # 開放 String;封閉 enum 保留給 Slice 4(見模組 docstring)。
    span_type = Column(String(64), nullable=False)
    name = Column(String(255), nullable=False)
    started_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)
    # ok / error / cancelled(doc 01;Pydantic 層封閉)。
    status = Column(String(20), nullable=False, default="ok",
                    server_default="ok")
    attributes = Column(JSONValue, nullable=True)
    # 五值:router/proxy/agent/model/studio(Pydantic 層封閉)。
    producer = Column(String(20), nullable=False)
    classification_level = Column(String(20), nullable=False,
                                  default="無機密", server_default="無機密")
    created_at = Column(DateTime, nullable=False, default=_utcnow)
