# -*- coding: utf-8 -*-
"""Task Service(Slice 2b-A)— Task / TaskRun 生命週期與 SourceSnapshot 編排。

依 doc 01(十值狀態機、SourceSnapshot 三規則、trace_id 必產生)與
doc 03(admin/owner 全域 bypass 必寫 audit)。風格對齊
``app/services/conversation_service.py``:module-level functions on Session。

SourceSnapshot 三規則落地:
1. ``create_task`` 一律寫 snapshot —— 有來源寫來源、無來源寫
   origin="none" 的「明確宣告無來源」列,並回填 ``task.source_snapshot_id``。
2. Citation 只指 snapshot 內 chunk(本 slice 無 citation 寫入面,由
   model 層 FK 缺席保證)。
3. snapshot classification = 來源可導出分類的 max;來源(現況
   ingestion collections/documents)尚無分類欄位時退回 無機密。
   payload 宣告的 ``classification_level`` 只作 *task* 分類的 floor
   (task = max(宣告, snapshot)),不灌入 snapshot(規則 3 純來源導出)。

錯誤語彙(供 router 對映 HTTP 碼):
- ``LookupError``     → 404(任務 / 使用者不存在)
- ``PermissionError`` → 403(非 requester 且非 admin tier)
- ``ValueError``      → 422 / 400(非法狀態轉移、未知 enum、宣告矛盾)
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.source_snapshot import SourceSnapshot
from app.models.task import Task, TaskRun
from app.models.user import User
from anila_contracts import Classification as ClassificationLevel
from app.schemas.contracts.tasks import (
    DispatchTarget,
    SnapshotOrigin,
    SourceScope,
    TaskCreate,
    TaskRunStatus,
    TaskStatus,
)
from app.services.audit_service import log_audit_event
from app.services.auth_service import is_admin_tier


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── 狀態機(doc 01 十值)────────────────────────────────────────────────────
# 合法轉移表;不在表內(含終態出邊與自轉移)一律非法 → ValueError。
_LEGAL_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.DRAFT: frozenset({TaskStatus.SUBMITTED, TaskStatus.CANCELLED}),
    TaskStatus.SUBMITTED: frozenset(
        {TaskStatus.POLICY_CHECKING, TaskStatus.CANCELLED}
    ),
    TaskStatus.POLICY_CHECKING: frozenset({
        TaskStatus.SOURCE_RESOLVING,
        TaskStatus.BLOCKED_BY_POLICY,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }),
    TaskStatus.SOURCE_RESOLVING: frozenset(
        {TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.RUNNING: frozenset({
        TaskStatus.WAITING_FOR_USER,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }),
    TaskStatus.WAITING_FOR_USER: frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCELLED}
    ),
    # 終態:completed / failed / cancelled / blocked_by_policy 無出邊。
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
    TaskStatus.BLOCKED_BY_POLICY: frozenset(),
}

# start_task_run 的快轉鏈:沿典型生命週期把 pre-running 任務推進到 running。
_RUNNING_FAST_FORWARD: dict[TaskStatus, TaskStatus] = {
    TaskStatus.DRAFT: TaskStatus.SUBMITTED,
    TaskStatus.SUBMITTED: TaskStatus.POLICY_CHECKING,
    TaskStatus.POLICY_CHECKING: TaskStatus.SOURCE_RESOLVING,
    TaskStatus.SOURCE_RESOLVING: TaskStatus.RUNNING,
    TaskStatus.WAITING_FOR_USER: TaskStatus.RUNNING,
}

_TERMINAL_RUN_STATUSES = frozenset({
    TaskRunStatus.COMPLETED,
    TaskRunStatus.FAILED,
    TaskRunStatus.CANCELLED,
})


# ── 內部 helpers ─────────────────────────────────────────────────────────────

def _derive_snapshot_classification(
    db: Session, payload: TaskCreate
) -> ClassificationLevel:
    """規則 3:snapshot 分類 = 來源可導出分類的 max,否則 無機密。

    現況 ingestion collections / documents 尚無 classification 欄位,
    以 ``getattr`` 前瞻式取值:欄位補上後本函式自動生效,現在則
    fail-safe 落在 無機密(migration floor,同 doc 08 backfill 精神)。

    Task API is ANILALM-facing: only ``origin='anilalm'`` collections
    participate (wrong-origin ids are rejected earlier in ``create_task``).
    """
    derived: list[ClassificationLevel] = []
    if payload.selected_collection_ids:
        from app.models.ingestion import IngestionCollection
        from app.api.ingestion.surface import SURFACE_ANILALM

        rows = (
            db.query(IngestionCollection)
            .filter(
                IngestionCollection.id.in_(payload.selected_collection_ids),
                IngestionCollection.origin == SURFACE_ANILALM,
            )
            .all()
        )
        for row in rows:
            raw = getattr(row, "classification_level", None)
            if raw:
                derived.append(ClassificationLevel.from_storage(raw))
    if not derived:
        return ClassificationLevel.UNCLASSIFIED
    return ClassificationLevel.max_of(derived)


def _snapshot_origin(payload: TaskCreate) -> SnapshotOrigin:
    if payload.selected_collection_ids:
        return SnapshotOrigin.COLLECTION
    if payload.selected_service_id:
        return SnapshotOrigin.SERVICE
    return SnapshotOrigin.NONE


# ── Task CRUD ────────────────────────────────────────────────────────────────

def create_task(
    db: Session, *, requester_user_id: int, payload: TaskCreate
) -> Task:
    """建立 Task + 對應 SourceSnapshot(規則 1:必指向 snapshot 或明確
    宣告無來源 —— 兩者都以一筆 snapshot 落地)。

    - trace_id 由 model default 產生(doc 01 驗收 2)。
    - 初始狀態 = draft(doc 01 狀態機起點)。
    - task.classification_level = max(payload 宣告, snapshot 導出分類)。
    """
    requester = db.query(User).filter(User.id == requester_user_id).first()
    if requester is None:
        raise LookupError(f"找不到使用者 id={requester_user_id}")

    declares_sources = bool(
        payload.selected_collection_ids or payload.selected_service_id
    )
    if payload.source_scope == SourceScope.NONE and declares_sources:
        raise ValueError(
            "source_scope=none 與已選來源矛盾:宣告無來源就不得夾帶 "
            "collection / service"
        )

    if payload.selected_collection_ids:
        from app.models.ingestion import IngestionCollection
        from app.api.ingestion.surface import SURFACE_ANILALM

        # Retention takes the same collection row lock while revalidating a
        # document erase.  Taking it here makes Task admission serialize with
        # that decision instead of allowing a new active Task to appear in the
        # erase check/unlink window.
        #
        # Product surface: /api/tasks is ANILALM-facing. A CSP-origin id is
        # treated as missing (same as personal collection shelf).
        selected_collections = (
            db.query(IngestionCollection)
            .filter(
                IngestionCollection.id.in_(payload.selected_collection_ids),
                IngestionCollection.origin == SURFACE_ANILALM,
            )
            .order_by(IngestionCollection.id)
            .with_for_update().populate_existing()
            .all()
        )
        found_ids = {row.id for row in selected_collections}
        missing = [
            cid
            for cid in payload.selected_collection_ids
            if cid not in found_ids
        ]
        if missing:
            raise LookupError(f"Collection {missing[0]} not found")
        inactive_ids = [
            row.id
            for row in selected_collections
            if row.lifecycle_state != "active"
        ]
        if inactive_ids:
            raise ValueError(
                "不得以非 active collection 建立 Task: "
                + ", ".join(str(collection_id) for collection_id in inactive_ids)
            )

    snapshot_level = _derive_snapshot_classification(db, payload)
    task_level = ClassificationLevel.max_of(
        [payload.classification_level, snapshot_level]
    )

    task = Task(
        title=payload.title,
        task_type=payload.task_type.value,
        requester_user_id=requester.id,
        department_id=getattr(requester, "department_id", None),
        conversation_id=payload.conversation_id,
        source_scope=payload.source_scope.value,
        selected_collection_ids=list(payload.selected_collection_ids),
        selected_service_id=payload.selected_service_id,
        requested_output_type=(
            payload.requested_output_type.value
            if payload.requested_output_type
            else None
        ),
        classification_level=task_level.to_storage(),
    )
    db.add(task)
    db.flush()  # 取得 task.id 給 snapshot FK

    snapshot = SourceSnapshot(
        task_id=task.id,
        origin=_snapshot_origin(payload).value,
        source_scope=payload.source_scope.value,
        collection_ids=list(payload.selected_collection_ids),
        classification_level=snapshot_level.to_storage(),
    )
    db.add(snapshot)
    db.flush()

    task.source_snapshot_id = snapshot.id
    db.commit()
    db.refresh(task)
    return task


def get_task(db: Session, task_id: int) -> Task | None:
    return db.query(Task).filter(Task.id == task_id).first()


def list_tasks(
    db: Session,
    *,
    requester_user_id: int,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
) -> list[Task]:
    """列出指定 requester 的任務(新→舊);status 未知值 fail-closed。"""
    q = db.query(Task).filter(Task.requester_user_id == requester_user_id)
    if status is not None:
        try:
            q = q.filter(Task.status == TaskStatus(status).value)
        except ValueError:
            raise ValueError(f"未知的任務狀態:{status!r}") from None
    return (
        q.order_by(Task.created_at.desc(), Task.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def ensure_task_access(db: Session, *, task_id: int, user_id: int) -> Task:
    """取任務並驗證存取權。

    - 任務不存在 → ``LookupError``(router 對映 404)。
    - 非 requester 且非 admin tier → ``PermissionError``(403)。
    - admin/owner 全域 bypass(doc 03 拍板)—— 但跨界存取必寫 audit
      (actor / action / resource)。
    """
    task = get_task(db, task_id)
    if task is None:
        raise LookupError(f"找不到任務 id={task_id}")
    if task.requester_user_id == user_id:
        return task

    user = db.query(User).filter(User.id == user_id).first()
    if user is not None and is_admin_tier(user):
        log_audit_event(
            db,
            action="task.admin_access",
            resource_type="task",
            resource_id=task.id,
            actor=user,
            detail=f"admin tier 跨界存取任務 {task.id}"
                   f"(requester={task.requester_user_id})",
            commit=True,
        )
        return task
    raise PermissionError(f"使用者 {user_id} 無權存取任務 {task_id}")


# ── 狀態機 ────────────────────────────────────────────────────────────────────

def transition_task(
    db: Session, *, task: Task, new_status: str, commit: bool = True
) -> Task:
    """套用一次狀態轉移;未知狀態或非法轉移一律 ``ValueError``。"""
    try:
        target = TaskStatus(new_status)
    except ValueError:
        raise ValueError(f"未知的任務狀態:{new_status!r}") from None
    current = TaskStatus(task.status)
    if target not in _LEGAL_TRANSITIONS[current]:
        raise ValueError(
            f"非法狀態轉移:{current.value} → {target.value}"
        )
    task.status = target.value
    task.updated_at = _utcnow()
    db.flush()
    if commit:
        db.commit()
        db.refresh(task)
    return task


# ── TaskRun 生命週期 ──────────────────────────────────────────────────────────

def start_task_run(
    db: Session, *, task: Task, dispatch_target: str, commit: bool = True
) -> TaskRun:
    """開一筆執行(run_sequence 遞增),並把任務推進到 running。

    任務若在 pre-running 狀態(draft/submitted/policy_checking/
    source_resolving/waiting_for_user),沿合法鏈快轉到 running;
    終態任務不可再開 run → ``ValueError``。
    """
    # This service is the canonical allocator, including callers outside the
    # HTTP proxy.  Always acquire the parent Task first and refresh any stale
    # identity-map state before inspecting status or allocating a sequence.
    task = (
        db.query(Task)
        .populate_existing()
        .filter(Task.id == task.id)
        .with_for_update()
        .one_or_none()
    )
    if task is None:
        raise ValueError("找不到 Task，無法建立 TaskRun")

    active_run = (
        db.query(TaskRun.id)
        .filter(
            TaskRun.task_id == task.id,
            TaskRun.status.in_((
                TaskRunStatus.QUEUED.value,
                TaskRunStatus.RUNNING.value,
            )),
        )
        .first()
    )
    if active_run is not None:
        raise ValueError(f"Task#{task.id} 已有 active TaskRun")

    try:
        target = DispatchTarget(dispatch_target)
    except ValueError:
        raise ValueError(f"未知的派發目的地:{dispatch_target!r}") from None

    current = TaskStatus(task.status)
    while current != TaskStatus.RUNNING:
        nxt = _RUNNING_FAST_FORWARD.get(current)
        if nxt is None:
            raise ValueError(
                f"任務狀態 {current.value} 不可開始執行(終態或非法路徑)"
            )
        task = transition_task(
            db, task=task, new_status=nxt.value, commit=False
        )
        current = TaskStatus(task.status)

    last_sequence = (
        db.query(TaskRun.run_sequence)
        .filter(TaskRun.task_id == task.id)
        .order_by(TaskRun.run_sequence.desc())
        .limit(1)
        .scalar()
    ) or 0
    run = TaskRun(
        task_id=task.id,
        run_sequence=last_sequence + 1,
        dispatch_target=target.value,
        status=TaskRunStatus.RUNNING.value,
        started_at=_utcnow(),
        classification_level=task.classification_level,
    )
    db.add(run)
    db.flush()
    if commit:
        db.commit()
        db.refresh(run)
    return run


def finish_task_run(
    db: Session,
    *,
    task_run: TaskRun,
    status: str,
    error: dict | None = None,
    usage_record_id: int | None = None,
    commit: bool = True,
) -> TaskRun:
    """收攏一筆執行:只接受終態(completed/failed/cancelled)。

    同時收斂 Task 本身；run 與 task 在同一個 transaction flush/commit，
    避免 run 已終態但 Task 永遠停在 running。policy deny 的
    ``blocked_by_policy`` 由治理 finalizer 明示處理（run 仍為 failed）。
    """
    try:
        target = TaskRunStatus(status)
    except ValueError:
        raise ValueError(f"未知的執行狀態:{status!r}") from None
    if target not in _TERMINAL_RUN_STATUSES:
        raise ValueError(f"執行只能收攏到終態,收到:{target.value}")
    task_run.status = target.value
    task_run.finished_at = _utcnow()
    task_run.error = error
    if usage_record_id is not None:
        task_run.usage_record_id = usage_record_id
    task = db.get(Task, task_run.task_id)
    if task is None:
        raise ValueError(f"找不到執行所屬任務 id={task_run.task_id}")
    if target == TaskRunStatus.COMPLETED:
        task.status = TaskStatus.COMPLETED.value
    elif target == TaskRunStatus.FAILED:
        task.status = TaskStatus.FAILED.value
    else:
        task.status = TaskStatus.CANCELLED.value
    task.updated_at = _utcnow()
    db.flush()
    if commit:
        db.commit()
        db.refresh(task_run)
    return task_run
