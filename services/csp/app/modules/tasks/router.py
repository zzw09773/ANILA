# -*- coding: utf-8 -*-
"""Task API(Slice 2b-A)— JWT 認證的 /api/tasks 讀寫面。

doc 09 §Task API 明定 ``POST /api/tasks`` 與 ``GET /api/tasks/{task_id}``
(本檔實作);``/submit`` ``/cancel`` ``/events`` ``/trace`` 屬後續 slice。
``GET /api/tasks``(自己的清單)與 ``GET /api/tasks/{task_id}/runs`` 為
Slice 2b-A 附加讀面。差異註記:doc 09 create body 例含 ``input``(text),
但 Slice 2a 凍結的 ``TaskCreate`` 契約與 ``tasks`` 表皆無該欄 —— 互動文字
由 doc 01 的 TaskMessage 承載,屬後續 slice,本檔遵循已凍結契約。

邊界:本 module 不 import ``app.api``;auth 依賴直接取自
``app.services.auth_service``(與 ``app.api.auth`` re-export 同一實體)。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.schemas.contracts.tasks import TaskCreate, TaskOut, TaskRunOut, TaskType
from app.services.audit_service import log_audit_event
from app.services.auth_service import get_current_user, is_admin_tier
from app.services.inference_audit import record_at_acceptance, record_at_outcome

from . import service

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class TaskCancelOut(BaseModel):
    task_id: int
    accepted: bool
    status: str


def _load_task_or_http(db: Session, task_id: int, user: User):
    """service 例外 → HTTP 碼(zh-TW detail)。"""
    try:
        return service.ensure_task_access(db, task_id=task_id, user_id=user.id)
    except LookupError:
        raise HTTPException(status_code=404, detail="找不到此任務") from None
    except PermissionError:
        raise HTTPException(status_code=403, detail="無權存取此任務") from None


@router.post("", response_model=TaskOut, status_code=201)
def create_task(
    payload: TaskCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """建立任務(requester = 當前使用者);必產生 trace_id 與 snapshot。"""
    studio_resource_id = (
        payload.requested_output_type.value
        if payload.requested_output_type is not None
        else "generate_artifact"
    )
    studio_meta = {
        "task_type": payload.task_type.value,
        "requested_output_type": (
            payload.requested_output_type.value
            if payload.requested_output_type is not None
            else None
        ),
        "collection_ids": list(payload.selected_collection_ids or []),
    }
    # Strict mode: durable acceptance write BEFORE create_task so a 503
    # leaves no unaudited persisted task. Non-strict keeps outcome-point
    # ordering (after create, with task_id).
    acceptance_recorded = False
    if payload.task_type == TaskType.GENERATE_ARTIFACT:
        acceptance_recorded = record_at_acceptance(
            db,
            request=request,
            actor=current_user,
            action="inference.studio",
            resource_id=studio_resource_id,
            detail=payload.title,
            metadata=studio_meta,
            commit=True,
            stream=False,
        )
    try:
        task = service.create_task(
            db, requester_user_id=current_user.id, payload=payload
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    log_audit_event(
        db,
        action="task.created",
        resource_type="task",
        resource_id=task.id,
        actor=current_user,
        detail=f"建立任務「{task.title}」(type={task.task_type})",
        metadata={"trace_id": task.trace_id, "task_type": task.task_type},
        commit=True,
    )
    if payload.task_type == TaskType.GENERATE_ARTIFACT:
        record_at_outcome(
            db,
            request=request,
            actor=current_user,
            action="inference.studio",
            resource_id=studio_resource_id,
            detail=payload.title,
            status="success",
            metadata={**studio_meta, "task_id": task.id},
            commit=True,
            acceptance_recorded=acceptance_recorded,
        )
    return task


@router.get("", response_model=list[TaskOut])
def list_tasks(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None),
    user_id: int | None = Query(None, description="admin tier 專用的使用者過濾"),
):
    """列出自己的任務;admin/owner 可用 ``user_id`` 過濾他人任務。"""
    requester_id = current_user.id
    if user_id is not None and user_id != current_user.id:
        if not is_admin_tier(current_user):
            raise HTTPException(
                status_code=403, detail="僅管理員可查詢其他使用者的任務"
            )
        requester_id = user_id
    try:
        return service.list_tasks(
            db,
            requester_user_id=requester_id,
            limit=limit,
            offset=offset,
            status=status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/{task_id}", response_model=TaskOut)
def get_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _load_task_or_http(db, task_id, current_user)


@router.get("/{task_id}/runs", response_model=list[TaskRunOut])
def list_task_runs(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = _load_task_or_http(db, task_id, current_user)
    return task.runs  # relationship 已按 run_sequence 排序


@router.post(
    "/{task_id}/cancel",
    response_model=TaskCancelOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Signal the live downstream stream; durable recovery is Gate 5 scope."""
    task = _load_task_or_http(db, task_id, current_user)
    if task.status in {"completed", "failed", "cancelled", "blocked_by_policy"}:
        return TaskCancelOut(task_id=task.id, accepted=False, status=task.status)

    # Import locally so the Task domain stays independent of proxy internals.
    from app.services.proxy.cancellation import CancellationDisposition, registry

    cancel_result = await registry.cancel(task.id)
    accepted = cancel_result.accepted
    response_status = (
        "cancellation_requested"
        if cancel_result.disposition is CancellationDisposition.ACCEPTED
        else (
            "cancellation_in_progress"
            if cancel_result.in_progress
            else task.status
        )
    )
    log_audit_event(
        db,
        action="task.cancel_requested",
        resource_type="task",
        resource_id=task.id,
        actor=current_user,
        status="success" if accepted else "failure",
        detail=(
            "已通知執行中串流取消"
            if cancel_result.disposition is CancellationDisposition.ACCEPTED
            else (
                "取消已在處理中，保持串流連線等待終態"
                if cancel_result.in_progress
                else "目前沒有同程序執行中串流"
            )
        ),
        metadata={
            "cancel_disposition": cancel_result.disposition.value,
            # A duplicate IN_PROGRESS response is accepted for idempotency,
            # but does not send a second Event signal to the live stream.
            "in_session_signal_delivered": (
                cancel_result.disposition is CancellationDisposition.ACCEPTED
            ),
        },
        commit=True,
    )
    return TaskCancelOut(
        task_id=task.id,
        accepted=accepted,
        status=response_status,
    )
