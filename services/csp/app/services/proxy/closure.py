# -*- coding: utf-8 -*-
"""Durable Task-linked inference closure.

Formal Task traffic must not rely on the process-memory usage queue.  This
module closes one outbound call in one transaction: usage, Full Trace spans,
and (for the foreground call) Task/TaskRun terminal state.  A deterministic
per-call closure id makes an ambiguous commit retry idempotent.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session

from anila_contracts import Classification
from app.models.audit_log import AuditLog
from app.models.task import Task, TaskRun
from app.models.token_usage import TokenUsage
from app.models.trace_span import TraceSpan
from app.schemas.contracts.traces import SpanProducer

logger = logging.getLogger("app.services.proxy_service")


@dataclass(frozen=True)
class UsageRecordData:
    api_key_id: int | None
    user_id: int
    department_id: int | None
    model_id: int | None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    request_duration_ms: int | None = None
    conversation_id: str | None = None
    request_type: str = "chat"
    caller_agent_id: int | None = None
    caller_client_id: int | None = None


@dataclass(frozen=True)
class TaskCallClosure:
    closure_id: str
    task_id: int
    task_run_id: int
    trace_id: str
    started_at: datetime
    status: str
    is_agent: bool
    target_id: int | None
    target_name: str | None
    usage: UsageRecordData | None = None
    error: dict[str, Any] | None = None
    finalize_run: bool = True
    classification_level: str | None = None
    callsite: str | None = None


def new_closure_id() -> str:
    return uuid.uuid4().hex


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _is_transient_db_error(exc: BaseException) -> bool:
    if isinstance(exc, OperationalError):
        return True
    if not isinstance(exc, DBAPIError):
        return False
    if exc.connection_invalidated:
        return True
    original = getattr(exc, "orig", None)
    code = str(getattr(original, "pgcode", "") or getattr(original, "sqlstate", ""))
    return code.startswith("08") or code in {"40001", "40P01", "55P03"}


def _existing_closure(db: Session, closure: TaskCallClosure) -> int | None | bool:
    row = (
        db.query(TraceSpan)
        .filter(
            TraceSpan.trace_id == closure.trace_id,
            TraceSpan.span_id == closure.closure_id,
            TraceSpan.task_id == closure.task_id,
        )
        .one_or_none()
    )
    if row is None:
        return False
    attrs = row.attributes if isinstance(row.attributes, dict) else {}
    usage_id = attrs.get("usage_record_id")
    return int(usage_id) if isinstance(usage_id, int) else None


def _persist_once(db: Session, closure: TaskCallClosure) -> int | None:
    # Repository lock order is Task -> TaskRun.
    task = (
        db.query(Task)
        .filter(Task.id == closure.task_id)
        .populate_existing()
        .with_for_update()
        .one()
    )
    run = (
        db.query(TaskRun)
        .filter(
            TaskRun.id == closure.task_run_id,
            TaskRun.task_id == closure.task_id,
        )
        .populate_existing()
        .with_for_update()
        .one()
    )
    if task.trace_id != closure.trace_id:
        raise ValueError("Task closure trace ownership 不一致")

    existing = _existing_closure(db, closure)
    if existing is not False:
        db.commit()
        return existing if isinstance(existing, int) else None

    if run.status in ("completed", "failed", "cancelled"):
        raise RuntimeError("TaskRun 已終止，拒絕新增不完整 closure")

    level = Classification.max_of([
        Classification.from_storage(task.classification_level),
        Classification.from_storage(run.classification_level),
        Classification.from_storage(
            closure.classification_level or Classification.UNCLASSIFIED.value
        ),
    ])
    now = datetime.now(timezone.utc)
    usage_row: TokenUsage | None = None
    if closure.usage is not None:
        value = closure.usage
        usage_row = TokenUsage(
            api_key_id=value.api_key_id,
            user_id=value.user_id,
            department_id=value.department_id,
            model_id=value.model_id,
            prompt_tokens=value.prompt_tokens,
            completion_tokens=value.completion_tokens,
            total_tokens=value.total_tokens,
            request_timestamp=now,
            request_duration_ms=value.request_duration_ms,
            conversation_id=value.conversation_id,
            # Formal Task trace is authoritative; inbound trace headers never
            # replace it.
            trace_id=task.trace_id,
            request_type=value.request_type,
            caller_agent_id=value.caller_agent_id,
            caller_client_id=value.caller_client_id,
            task_id=task.id,
            legacy_runtime_call=False,
        )
        db.add(usage_row)
        db.flush()

    parent_attrs: dict[str, Any] = {
        "target_kind": "agent" if closure.is_agent else "model",
        "target_id": closure.target_id,
        "target_name": closure.target_name,
        "callsite": closure.callsite,
    }
    if usage_row is not None:
        parent_attrs["usage_record_id"] = usage_row.id
    child_attrs = dict(parent_attrs)
    if closure.usage is not None:
        child_attrs["usage"] = {
            "prompt_tokens": closure.usage.prompt_tokens,
            "completion_tokens": closure.usage.completion_tokens,
            "total_tokens": closure.usage.total_tokens,
        }
    span_status = "ok" if closure.status == "completed" else "error"
    db.add_all([
        TraceSpan(
            trace_id=task.trace_id,
            span_id=closure.closure_id,
            task_id=task.id,
            span_type="agent.run.finished",
            name="proxy.dispatch",
            started_at=_naive_utc(closure.started_at),
            ended_at=_naive_utc(now),
            status=span_status,
            attributes=parent_attrs,
            producer=SpanProducer.PROXY.value,
            classification_level=level.to_storage(),
        ),
        TraceSpan(
            trace_id=task.trace_id,
            span_id=f"{closure.closure_id[:48]}-child",
            parent_span_id=closure.closure_id,
            task_id=task.id,
            span_type=(
                "agent.tool_call.finished"
                if closure.is_agent
                else "agent.model_call.finished"
            ),
            name="agent.call" if closure.is_agent else "model.call",
            started_at=_naive_utc(closure.started_at),
            ended_at=_naive_utc(now),
            status=span_status,
            attributes=child_attrs,
            producer=SpanProducer.PROXY.value,
            classification_level=level.to_storage(),
        ),
    ])

    if closure.finalize_run:
        # Preserve the complete terminal state.  ``cancelled`` is a first-class
        # in-session outcome in Gate 4, not an error alias: collapsing it to
        # ``failed`` would make the Task/TaskRun ledger disagree with the
        # trusted ``anila.step`` cancelled terminal emitted by the bridge.
        if closure.status not in ("completed", "failed", "cancelled"):
            raise ValueError(f"非法 Task closure 終態: {closure.status!r}")
        run.status = closure.status
        run.finished_at = now
        run.error = closure.error
        run.usage_record_id = usage_row.id if usage_row is not None else None
        task.status = run.status
        task.updated_at = now
        db.add(AuditLog(
            actor_user_id=task.requester_user_id,
            action="task.run.finished",
            resource_type="task",
            resource_id=str(task.id),
            status=(
                "success"
                if run.status in ("completed", "cancelled")
                else "failure"
            ),
            detail=f"task_run={run.id}; terminal={run.status}; closure={closure.closure_id}",
        ))
    db.flush()
    db.commit()
    return usage_row.id if usage_row is not None else None


def persist_task_call_closure(
    db: Session, closure: TaskCallClosure, *, max_attempts: int = 3
) -> int | None:
    """Persist an idempotent closure, retrying only transient DB failures."""
    for attempt in range(1, max_attempts + 1):
        try:
            return _persist_once(db, closure)
        except Exception as exc:
            db.rollback()
            if not _is_transient_db_error(exc) or attempt >= max_attempts:
                raise
            logger.warning(
                "Task closure 暫時性 DB 錯誤，重試 %s/%s closure=%s",
                attempt,
                max_attempts,
                closure.closure_id,
            )
            time.sleep(0.01 * attempt)
    raise AssertionError("unreachable")
