# -*- coding: utf-8 -*-
"""Proxy self-emitted Full Trace spans (Slice 4a — doc 05 §6 / doc 10 §6 P0).

When the CSP data plane proxies a *task-linked* call (Slice 2b-C: a TaskRun
brackets the dispatch), it emits durable ``trace_spans`` rows IN-PROCESS —
a direct DB write, never an HTTP self-call — so the Full Trace pipeline finally
has a real producer for proxied model / agent calls. Legacy task-less traffic
stays untraced (正式 task 必須有 trace —— 無 trace context 就不產 span)。

span_type mapping (proxy conceptual name → doc 05 §6 required-13 span type,
逐字):

    proxy.dispatch  (整段代理呼叫的邊界)   → ``agent.run.finished``
    model.call      (上游 LLM 呼叫)         → ``agent.model_call.finished``
    agent.call      (上游 agent 派發)        → ``agent.tool_call.finished``

理由:required-13 全是「agent run 內部」語彙,proxy 沒有原生型別。整段代理
呼叫是 run 邊界;純模型上游是一次 model call;派發到子 agent 則以「被呼叫的
能力」(tool call)建模。只用終態 ``.finished`` 事件,因為 proxy 是在
finalization 時一次性寫入完整區間(start+end)與最終狀態。

韌性:每次寫入開自己的短命 session、log-not-raise,與
``task_link.finalize_task_run`` 同一姿態 —— trace 寫入的錯誤絕不可打斷已送出
的回應 / SSE teardown。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app.database import SessionLocal
from app.models.trace_span import TraceSpan
from app.models.task import Task
from anila_contracts import Classification as ClassificationLevel
from app.schemas.contracts.traces import SpanProducer

# Share the proxy logger channel so span-emission logs route with the rest of
# the proxy pipeline.
logger = logging.getLogger("app.services.proxy_service")

# proxy 概念名 → doc 05 §6 逐字 required span type。
SPAN_TYPE_PROXY_DISPATCH = "agent.run.finished"
SPAN_TYPE_MODEL_CALL = "agent.model_call.finished"
SPAN_TYPE_AGENT_CALL = "agent.tool_call.finished"


def _naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize to naive UTC so persisted timestamps stay comparable.

    The ``trace_spans.started_at`` column is a tz-naive ``DateTime``; mixing
    tz-aware and tz-naive values would break the ordering sort in
    ``GET /api/traces/{trace_id}``. Ingest normalizes the same way.
    """
    if dt is not None and dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def emit_span(
    db_sessionmaker,
    *,
    trace_id: str,
    span_type: str,
    name: str,
    started_at: Optional[datetime],
    ended_at: Optional[datetime],
    status: str,
    parent_span_id: Optional[str] = None,
    task_id: Optional[int] = None,
    attributes: Optional[dict[str, Any]] = None,
    classification_level: str | None = None,
) -> Optional[str]:
    """Persist one proxy-produced span and return its generated ``span_id``.

    Opens an independent session via ``db_sessionmaker`` (spans are emitted at
    finalization, outside the request-scoped session — same posture as
    ``finalize_task_run``). Each call mints a fresh uuid ``span_id`` so
    ``(trace_id, span_id)`` never collides. Failures are logged, never raised;
    returns ``None`` on any error so a caller chaining a child span degrades to
    a parentless span rather than crashing the proxied flow.
    """
    if not trace_id:
        return None
    span_id = uuid.uuid4().hex
    db = db_sessionmaker()
    try:
        if task_id is None:
            return None
        task = db.get(Task, task_id)
        if task is None or task.trace_id != trace_id:
            raise ValueError("proxy span 的 task/trace ownership 不一致")
        effective_level = ClassificationLevel.max_of([
            ClassificationLevel.from_storage(task.classification_level),
            ClassificationLevel.from_storage(
                classification_level or ClassificationLevel.UNCLASSIFIED.value
            ),
        ])
        db.add(
            TraceSpan(
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                task_id=task_id,
                span_type=span_type,
                name=name,
                started_at=_naive_utc(started_at),
                ended_at=_naive_utc(ended_at),
                status=status,
                attributes=attributes,
                producer=SpanProducer.PROXY.value,
                classification_level=effective_level.to_storage(),
            )
        )
        db.commit()
        return span_id
    except Exception:
        db.rollback()
        logger.exception(
            "emit_span 失敗 trace_id=%s span_type=%s", trace_id, span_type
        )
        return None
    finally:
        db.close()


def record_proxy_dispatch(
    *,
    trace_id: Optional[str],
    task_id: Optional[int],
    started_at: datetime,
    ended_at: datetime,
    status: str,
    is_agent: bool,
    target_id: Optional[int] = None,
    target_name: Optional[str] = None,
    usage: Optional[dict[str, Any]] = None,
    db_sessionmaker=None,
) -> None:
    """Emit the ``proxy.dispatch`` parent span + its model/agent child span.

    No-op when ``trace_id`` is falsy (legacy task-less traffic carries no trace
    context). ``status`` is ``ok`` on success / ``error`` on upstream failure,
    mirrored onto both spans. Best-effort — delegates to ``emit_span`` which
    logs-not-raises; a failed parent emit degrades the child to parentless.
    """
    if not trace_id:
        return
    # Resolved at call time from the module global so tests can monkeypatch
    # ``spans.SessionLocal`` at the test engine (mirrors task_link.SessionLocal).
    maker = db_sessionmaker or SessionLocal

    target_kind = "agent" if is_agent else "model"
    parent_attrs: dict[str, Any] = {"target_kind": target_kind}
    if target_id is not None:
        parent_attrs["target_id"] = target_id
    if target_name is not None:
        parent_attrs["target_name"] = target_name

    parent_span_id = emit_span(
        maker,
        trace_id=trace_id,
        span_type=SPAN_TYPE_PROXY_DISPATCH,
        name="proxy.dispatch",
        started_at=started_at,
        ended_at=ended_at,
        status=status,
        task_id=task_id,
        attributes=parent_attrs,
    )

    child_attrs: dict[str, Any] = dict(parent_attrs)
    if usage:
        child_attrs["usage"] = usage
    emit_span(
        maker,
        trace_id=trace_id,
        span_type=SPAN_TYPE_AGENT_CALL if is_agent else SPAN_TYPE_MODEL_CALL,
        name="agent.call" if is_agent else "model.call",
        started_at=started_at,
        ended_at=ended_at,
        status=status,
        parent_span_id=parent_span_id,
        task_id=task_id,
        attributes=child_attrs,
    )
