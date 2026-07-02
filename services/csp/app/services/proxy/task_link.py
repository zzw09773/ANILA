"""Slice 2b-C — task_id wiring for the CSP data plane (/v1/chat/completions).

Doc 04 §5 / doc 10 Slice 2: every ``/v1/chat/completions`` call MAY carry an
``X-ANILA-Task-Id`` header. When present, the call is validated against the
Task spine (``app.modules.tasks``), a ``PolicyDecision(action="task.run")``
row is recorded (``app.modules.policy``), and a ``TaskRun`` brackets the
proxied call (started before dispatch, finished on completion / failure).
When absent, behavior is unchanged except the usage row is marked
``legacy_runtime_call=true`` (doc 10 Slice 2 Done).

SECURITY:
- Caller auth is NOT weakened: every request still passes ``get_caller``
  (JWT / sk- api key). The task header only ADDS checks.
- Service-token caller (Router callback): the acting user is resolved from
  the inbound ``X-ANILA-User-Id`` employee id ONLY after the presented
  ``X-CSP-Service-Token`` verifies against the credential store
  (fail-closed 401). The task's requester must match the forwarded
  identity (mismatch → 403). This mirrors the established
  service-token + trusted-forwarded-user-headers pattern
  (see app/models/token_usage.py caller attribution notes).

``app.modules.tasks`` / ``app.modules.policy`` are imported lazily at call
time — they are module-boundary packages (doc 10 §4) landing in parallel
slices; only their package-root public surface is used.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.task import TaskRun
from app.models.user import User
from app.schemas.contracts.policy import (
    PolicyAction,
    PolicyActorType,
    PolicyDecisionVerdict,
)
from app.services import agent_credential_service
from app.services.proxy.headers import _EMPLOYEE_ID_RE

logger = logging.getLogger(__name__)

TASK_ID_HEADER = "X-ANILA-Task-Id"
_TERMINAL_RUN_STATUSES = ("completed", "failed", "cancelled")


@dataclass(frozen=True)
class TaskRunContext:
    """Plain-value snapshot of the started run (safe to carry across the
    request/stream boundary — no live ORM state)."""

    task_id: int
    trace_id: str
    task_run_id: int


def _resolve_acting_user(db: Session, *, caller, request_headers):
    """Return (acting_user, actor_type, actor_id) for the task check.

    Default: the authenticated caller acts for themselves. When the request
    presents an ``X-CSP-Service-Token`` (Router callback), the token must
    verify (fail-closed) and the acting user is resolved from the forwarded
    ``X-ANILA-User-Id`` employee id (card branch: ``username`` IS the 員編,
    same shape rule as ``downstream_identity``).
    """
    service_token = request_headers.get("X-CSP-Service-Token")
    if not service_token:
        return caller.user, PolicyActorType.USER.value, caller.user.id

    identity = agent_credential_service.verify_service_token(
        db, token=service_token
    )
    if identity is None:
        raise HTTPException(status_code=401, detail="無效的 service token")

    employee_id = (request_headers.get("X-ANILA-User-Id") or "").strip()
    if not employee_id or not _EMPLOYEE_ID_RE.match(employee_id):
        raise HTTPException(
            status_code=403,
            detail="服務呼叫帶任務時必須轉發有效的 X-ANILA-User-Id(員編)",
        )
    acting_user = (
        db.query(User)
        .filter(User.username == employee_id, User.is_active.is_(True))
        .first()
    )
    if acting_user is None:
        raise HTTPException(
            status_code=403, detail="轉發的員編查無對應的有效使用者"
        )
    actor_id = identity.agent_id or identity.service_client_id
    return acting_user, PolicyActorType.SERVICE.value, actor_id


def begin_task_run(
    db: Session,
    *,
    caller,
    request_headers,
    dispatch_target: str,
    resource_type: str,
    resource_id: str,
) -> Optional[TaskRunContext]:
    """Validate the optional task header and open a TaskRun before dispatch.

    Returns ``None`` when no ``X-ANILA-Task-Id`` header is present (legacy
    traffic — behavior unchanged). Otherwise: resolve the acting user,
    enforce task access (404 unknown / 403 foreign), record the
    ``task.run`` PolicyDecision, start a TaskRun and move the task to
    ``running``. The run/decision rows are committed so the out-of-request
    finalizer (fresh session) can see them.
    """
    raw = request_headers.get(TASK_ID_HEADER)
    if raw is None or not str(raw).strip():
        return None
    try:
        task_id = int(str(raw).strip())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="X-ANILA-Task-Id 標頭格式錯誤(須為任務整數編號)",
        )

    acting_user, actor_type, actor_id = _resolve_acting_user(
        db, caller=caller, request_headers=request_headers
    )

    # Parallel-slice surfaces (module-boundary packages) — call-time import.
    from app.modules.policy import record_decision
    from app.modules.tasks import (
        ensure_task_access,
        start_task_run,
        transition_task,
    )

    try:
        task = ensure_task_access(db, task_id=task_id, user_id=acting_user.id)
    except LookupError:
        raise HTTPException(status_code=404, detail="任務不存在")
    except PermissionError:
        raise HTTPException(status_code=403, detail="無權使用該任務")

    # Defence-in-depth for the Router-callback path: the forwarded identity
    # must BE the task requester, independent of whatever broader access
    # ensure_task_access may grant.
    if (
        actor_type == PolicyActorType.SERVICE.value
        and task.requester_user_id != acting_user.id
    ):
        raise HTTPException(
            status_code=403, detail="任務申請人與轉發的使用者身分不符"
        )

    record_decision(
        db,
        action=PolicyAction.TASK_RUN.value,
        resource_type=resource_type,
        resource_id=resource_id,
        decision=PolicyDecisionVerdict.ALLOW.value,
        actor_type=actor_type,
        actor_id=actor_id,
        task_id=task.id,
    )
    # start_task_run fast-forwards the task to ``running`` along the legal
    # transition chain and rejects terminal-state tasks with ValueError
    # (tasks module state machine) → surface as 409.
    try:
        run = start_task_run(db, task=task, dispatch_target=dispatch_target)
    except ValueError as exc:
        raise HTTPException(
            status_code=409, detail=f"任務狀態不允許執行:{exc}"
        )

    if task.status != "running":
        try:
            transition_task(db, task=task, new_status="running")
        except Exception:
            # 狀態機拒絕不阻斷 run 本身 — run 的生命週期照常記錄,
            # 任務層狀態由 tasks 模組的規則決定。
            logger.warning(
                "task %s 狀態 %s → running 轉換被狀態機拒絕(run 照常執行)",
                task.id,
                task.status,
                exc_info=True,
            )

    # Durable BEFORE dispatch: a crashed proxy call must still leave the
    # decision + started run visible; the finalizer reads via a fresh session.
    db.commit()
    return TaskRunContext(
        task_id=task.id, trace_id=task.trace_id, task_run_id=run.id
    )


def finalize_task_run(
    task_run_id: Optional[int],
    status: str,
    *,
    error: Optional[dict] = None,
) -> None:
    """Mark the run completed / failed after the proxied call ends.

    Runs OUTSIDE the request-scoped session (streams finish after the
    handler returns), so it opens its own short-lived session. Idempotent —
    a run already in a terminal state is left untouched. Failures here are
    logged, never raised: finalization must not corrupt an already-sent
    response / SSE teardown (same resilience posture as the usage writer).
    """
    if task_run_id is None:
        return
    from app.modules.tasks import finish_task_run  # parallel-slice surface

    db = SessionLocal()
    try:
        run = db.query(TaskRun).filter(TaskRun.id == task_run_id).first()
        if run is None:
            logger.warning("finalize_task_run: 找不到 run id=%s", task_run_id)
            return
        if run.status in _TERMINAL_RUN_STATUSES:
            return
        finish_task_run(db, task_run=run, status=status, error=error)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(
            "finalize_task_run 失敗 run_id=%s status=%s", task_run_id, status
        )
    finally:
        db.close()
