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
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.audit_log import AuditLog
from app.models.task import Task
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
    commit: bool = True,
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
    )

    try:
        task = ensure_task_access(db, task_id=task_id, user_id=acting_user.id)
    except LookupError:
        raise HTTPException(status_code=404, detail="任務不存在")
    except PermissionError:
        raise HTTPException(status_code=403, detail="無權使用該任務")
    # Serialize run_sequence allocation and state transitions per Task.
    task = (
        db.query(Task).filter(Task.id == task.id).with_for_update().one()
    )

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

    decision_row = record_decision(
        db,
        action=PolicyAction.TASK_RUN.value,
        resource_type=resource_type,
        resource_id=resource_id,
        decision=PolicyDecisionVerdict.ALLOW.value,
        actor_type=actor_type,
        actor_id=actor_id,
        task_id=task.id,
        commit=False,
    )
    task.policy_decision_id = decision_row.id
    # start_task_run fast-forwards the task to ``running`` along the legal
    # transition chain and rejects terminal-state tasks with ValueError
    # (tasks module state machine) → surface as 409.
    try:
        run = start_task_run(
            db, task=task, dispatch_target=dispatch_target, commit=False
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409, detail=f"任務狀態不允許執行:{exc}"
        )

    if task.status != "running":
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="TaskRun 建立後 Task 未能原子轉入 running，已回滾",
        )

    # G6b: decision + Task/TaskRun transition + audit are one transaction.
    # A crash before commit leaves none of them; a crash after commit leaves a
    # durable running attempt which the reconciliation checker can close.
    db.add(AuditLog(
        actor_user_id=acting_user.id,
        actor_username=acting_user.username,
        action="task.run.started",
        resource_type="task",
        resource_id=str(task.id),
        status="success",
        detail=f"task_run={run.id}; target={dispatch_target}",
    ))
    db.flush()
    if commit:
        db.commit()
    return TaskRunContext(
        task_id=task.id, trace_id=task.trace_id, task_run_id=run.id
    )


def record_task_policy_decision(
    db: Session,
    *,
    task_ctx: TaskRunContext,
    action: str,
    resource_type: str,
    resource_id: str,
    decision: str,
    actor_id: str,
    reason: str | None = None,
    metadata: dict | None = None,
    block: bool = False,
    fail: bool = False,
    commit: bool = True,
) -> None:
    """Persist a runtime decision with its Task/Run/Audit state atomically.

    ``block=True`` is the sole proxy policy-deny transition and makes
    ``BLOCKED_BY_POLICY`` reachable.  The TaskRun uses ``failed`` because its
    closed vocabulary has no policy-blocked value; the structured error keeps
    the distinction. ``fail=True`` closes a technical pre-dispatch failure
    without mislabelling it as policy-blocked. A retry is idempotent once the
    run is terminal.
    """
    from app.modules.policy import record_decision

    task = (
        db.query(Task).filter(Task.id == task_ctx.task_id).with_for_update().one()
    )
    run = (
        db.query(TaskRun)
        .filter(TaskRun.id == task_ctx.task_run_id)
        .with_for_update()
        .one()
    )
    if run.status in _TERMINAL_RUN_STATUSES:
        # A concurrent cancel/finalize must be a hard stop for an allow path;
        # silently returning would let the caller dispatch after the ledger
        # had already closed the attempt. Deny retries stay idempotent because
        # the outbound is already rejected by the caller.
        if decision == PolicyDecisionVerdict.ALLOW.value:
            raise HTTPException(
                status_code=409,
                detail="TaskRun 已終止，禁止在終態後發出推論呼叫",
            )
        return
    if block and fail:
        raise ValueError("block and fail are mutually exclusive")
    row = record_decision(
        db,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        decision=decision,
        actor_type="user",
        actor_id=actor_id,
        task_id=task.id,
        reason=reason,
        metadata=metadata,
        commit=False,
    )
    now = datetime.now(timezone.utc)
    task.policy_decision_id = row.id
    task.updated_at = now
    if block or fail:
        task.status = "blocked_by_policy" if block else "failed"
        run.status = "failed"
        run.finished_at = now
        run.error = {
            "code": "classification_ceiling" if block else "pre_dispatch_failed",
            "message": reason,
        }
    db.add(AuditLog(
        actor_user_id=task.requester_user_id,
        action=(
            "task.policy.blocked" if block
            else "task.pre_dispatch.failed" if fail
            else "task.policy.allowed"
        ),
        resource_type="task",
        resource_id=str(task.id),
        status="failure" if block or fail else "success",
        detail=reason or f"{action} {decision}",
    ))
    db.flush()
    if commit:
        db.commit()


def finalize_task_preflight(
    db: Session,
    *,
    task_id: int,
    terminal_status: str,
    action: str,
    resource_type: str,
    resource_id: str | None,
    actor_id: str,
    reason: str,
) -> None:
    """Atomically close a Task rejected before a TaskRun could be opened.

    Canonical server retrieval and clearance execute before outbound dispatch.
    Their deny/failure paths therefore have no run to finalize.  This helper
    prevents the already-created Task from remaining ``draft`` and gives those
    paths the same PolicyDecision/Audit transaction as runtime denials.
    """
    if terminal_status not in ("failed", "blocked_by_policy"):
        raise ValueError("preflight terminal must be failed or blocked_by_policy")
    from app.modules.policy import record_decision

    task = db.query(Task).filter(Task.id == task_id).with_for_update().one()
    if task.status in ("completed", "failed", "cancelled", "blocked_by_policy"):
        return
    decision = record_decision(
        db,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        decision="deny",
        actor_type="user",
        actor_id=actor_id,
        task_id=task.id,
        reason=reason,
        metadata={"preflight_terminal": terminal_status},
        commit=False,
    )
    task.status = terminal_status
    task.policy_decision_id = decision.id
    task.updated_at = datetime.now(timezone.utc)
    db.add(AuditLog(
        actor_user_id=task.requester_user_id,
        action=f"task.preflight.{terminal_status}",
        resource_type="task",
        resource_id=str(task.id),
        status="failure",
        detail=reason,
    ))
    db.flush()
    db.commit()


def reconcile_stale_task_runs(
    db: Session, *, stale_after_seconds: int, limit: int = 100
) -> int:
    """Close attempts abandoned by process crash, atomically and retry-safe."""
    if stale_after_seconds < 60:
        raise ValueError("stale task-run threshold must be at least 60 seconds")
    from app.modules.policy import record_decision

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
    runs = (
        db.query(TaskRun)
        .filter(TaskRun.status == "running", TaskRun.started_at < cutoff)
        .order_by(TaskRun.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .all()
    )
    closed = 0
    for run in runs:
        task = (
            db.query(Task).filter(Task.id == run.task_id).with_for_update().one()
        )
        if run.status != "running":
            continue
        reason = "CSP 執行中斷且超過治理收斂時限，依 crash reconciliation 關閉"
        decision = record_decision(
            db,
            action="task.run",
            resource_type="task",
            resource_id=str(task.id),
            decision="deny",
            actor_type="service",
            actor_id="task-reconciler",
            task_id=task.id,
            reason=reason,
            metadata={"task_run_id": run.id, "reason_code": "stale_runtime"},
            commit=False,
        )
        now = datetime.now(timezone.utc)
        run.status = "failed"
        run.finished_at = now
        run.error = {"code": "stale_runtime", "message": reason}
        task.status = "failed"
        task.policy_decision_id = decision.id
        task.updated_at = now
        db.add(AuditLog(
            actor_user_id=task.requester_user_id,
            action="task.run.reconciled",
            resource_type="task",
            resource_id=str(task.id),
            status="failure",
            detail=reason,
        ))
        closed += 1
    if closed:
        db.flush()
        db.commit()
    return closed


def finalize_task_run_in_session(
    db: Session,
    task_run_id: Optional[int],
    status: str,
    *,
    error: Optional[dict] = None,
) -> bool:
    """Finalize using the caller's transaction and release its row locks.

    Returns true only when this call performed the terminal transition. The
    caller owns exception handling; a successful or idempotent call commits so
    any Task/TaskRun/model row locks held across outbound are released before
    another session can reconcile or administer the same rows.
    """
    if task_run_id is None:
        db.commit()
        return False
    if status not in (*_TERMINAL_RUN_STATUSES, "blocked_by_policy"):
        raise ValueError(f"非法治理終態: {status!r}")
    run_ref = db.query(TaskRun).filter(TaskRun.id == task_run_id).first()
    if run_ref is None:
        logger.warning("finalize_task_run: 找不到 run id=%s", task_run_id)
        db.commit()
        return False
    task = (
        db.query(Task)
        .filter(Task.id == run_ref.task_id)
        .with_for_update()
        .one()
    )
    run = (
        db.query(TaskRun)
        .filter(TaskRun.id == task_run_id)
        .with_for_update()
        .one()
    )
    if run.status in _TERMINAL_RUN_STATUSES:
        db.commit()
        return False
    now = datetime.now(timezone.utc)
    run.status = "failed" if status == "blocked_by_policy" else status
    run.finished_at = now
    run.error = error
    task.status = status
    task.updated_at = now
    db.add(AuditLog(
        actor_user_id=task.requester_user_id,
        action=(
            "task.run.blocked_by_policy"
            if status == "blocked_by_policy"
            else "task.run.finished"
        ),
        resource_type="task",
        resource_id=str(task.id),
        status="failure" if status in ("failed", "blocked_by_policy") else "success",
        detail=f"task_run={run.id}; terminal={status}",
    ))
    db.flush()
    db.commit()
    return True


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
    db = SessionLocal()
    try:
        finalize_task_run_in_session(
            db, task_run_id, status, error=error
        )
    except Exception:
        db.rollback()
        logger.exception(
            "finalize_task_run 失敗 run_id=%s status=%s", task_run_id, status
        )
    finally:
        db.close()
