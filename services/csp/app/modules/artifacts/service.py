# -*- coding: utf-8 -*-
"""Artifact Service(Slice 8a)—— artifact_jobs / artifacts / artifact_versions
/ export_records 的持久化、binding 規則與治理讀面。

依 doc 02(ArtifactJob 逐欄、§8 「Studio restart job 不丟失」)、doc 01
(Artifact / ArtifactVersion / ExportRecord、binding 規則)、doc 08(§5 四共通
分類欄位、§10 匯出判定)。

**模組邊界(independence)**:本 module 只碰 ``app.models`` 與
``app.schemas.contracts``,**不 import** ``app.modules.policy`` /
``app.modules.tasks`` / ``app.modules.launch``,也不 import ``app.api``。
分類的單向閂鎖(寫 ClassificationEvent)與 PolicyDecision 產出屬 policy
核心,由 ``app.api.artifacts`` 這個 orchestrator 呼叫 policy 完成;本 module
只負責 DB 落地、binding fail-closed 與 owner-scope 讀取。``inherited_level``
只讀來源列的等級供 orchestrator 計算 max,不自行閂鎖。

錯誤語彙(供 router 對映 HTTP 碼):
- ``LookupError``     → 404(job / artifact 不存在)
- ``PermissionError`` → 403(非 owner 且非 admin tier)
- ``ValueError``      → 422 / 409(binding 缺失、非法 job 狀態轉移)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.artifact import (
    Artifact,
    ArtifactJob,
    ArtifactVersion,
    ExportRecord,
)
from app.models.source_snapshot import SourceSnapshot
from app.models.task import Task
from app.models.user import User
from app.schemas.contracts.artifacts import (
    ArtifactExportIn,
    ArtifactIn,
    ArtifactJobIn,
    ArtifactJobPatch,
    ArtifactJobStatus,
)
from app.schemas.contracts.classification import ClassificationLevel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── job 狀態機(doc 02 四值)─────────────────────────────────────────────────
# 合法轉移:非終態允許自轉移(進度更新);終態(completed/failed)無出邊。
_LEGAL_JOB_TRANSITIONS: dict[ArtifactJobStatus, frozenset[ArtifactJobStatus]] = {
    ArtifactJobStatus.QUEUED: frozenset({
        ArtifactJobStatus.QUEUED,
        ArtifactJobStatus.RUNNING,
        ArtifactJobStatus.COMPLETED,
        ArtifactJobStatus.FAILED,
    }),
    ArtifactJobStatus.RUNNING: frozenset({
        ArtifactJobStatus.RUNNING,
        ArtifactJobStatus.COMPLETED,
        ArtifactJobStatus.FAILED,
    }),
    ArtifactJobStatus.COMPLETED: frozenset(),
    ArtifactJobStatus.FAILED: frozenset(),
}


# ── owner / binding 解析 ──────────────────────────────────────────────────────

def resolve_owner(
    db: Session, *, requester_user_id: int | None, employee_id: str | None
) -> tuple[int | None, str | None]:
    """把 requester_user_id 或員編解析成 (owner_user_id, employee_id)。

    員編騎在 ``users.username``(card 分支);查得到就回 users.id + 原始員編,
    查不到仍保留原始員編字串供稽核(owner_user_id=None)。
    """
    emp = (employee_id or "").strip() or None
    if requester_user_id is not None:
        return requester_user_id, emp
    if emp is not None:
        user = db.query(User).filter(User.username == emp).first()
        return (user.id if user is not None else None), emp
    return None, None


def inherited_level(
    db: Session, *, source_task_id: int | None, source_snapshot_id: int | None
) -> ClassificationLevel | None:
    """讀 task / snapshot 的分類等級,回其 max(doc 08 §5 傳遞公式的來源項)。

    只讀不閂鎖 —— 由 orchestrator 交給 policy 核心做單向 latch。兩者皆無
    → None(binding 規則另由 :func:`create_artifact` 把關)。
    """
    levels: list[ClassificationLevel] = []
    if source_task_id is not None:
        raw = db.query(Task.classification_level).filter(
            Task.id == source_task_id
        ).scalar()
        if raw:
            levels.append(ClassificationLevel.from_storage(raw))
    if source_snapshot_id is not None:
        raw = db.query(SourceSnapshot.classification_level).filter(
            SourceSnapshot.id == source_snapshot_id
        ).scalar()
        if raw:
            levels.append(ClassificationLevel.from_storage(raw))
    if not levels:
        return None
    return ClassificationLevel.max_of(levels)


# ── ArtifactJob(冪等 upsert + 狀態機)─────────────────────────────────────────

def register_job(
    db: Session, *, payload: ArtifactJobIn, owner_user_id: int | None,
    employee_id: str | None,
) -> ArtifactJob:
    """冪等 upsert(以 ``job_id``):存在則覆寫欄位、不存在則新增。"""
    job = db.get(ArtifactJob, payload.job_id)
    fields = dict(
        owner_user_id=owner_user_id,
        requester_employee_id=employee_id,
        collection_id=payload.collection_id,
        task_id=payload.task_id,
        source_snapshot_id=payload.source_snapshot_id,
        artifact_type=payload.artifact_type.value,
        status=payload.status.value,
        progress=payload.progress,
        params_digest=payload.params_digest,
        message=payload.message,
        trace_id=payload.trace_id,
        expires_at=payload.expires_at,
    )
    if job is None:
        job = ArtifactJob(job_id=payload.job_id, **fields)
        db.add(job)
    else:
        for key, value in fields.items():
            setattr(job, key, value)
        job.updated_at = _utcnow()
    db.commit()
    db.refresh(job)
    return job


def transition_job(
    db: Session, *, job_id: str, patch: ArtifactJobPatch
) -> ArtifactJob:
    """套用 job 狀態轉移 + 進度/回填;非法轉移 ``ValueError``、查無 ``LookupError``。

    非法轉移(終態出邊 / 逆向)→ ``ValueError``(router 對映 409);未知
    status 值在契約層已被 422 擋下,故此處只判轉移合法性。
    """
    job = db.get(ArtifactJob, job_id)
    if job is None:
        raise LookupError(f"找不到 artifact job {job_id}")
    current = ArtifactJobStatus(job.status)
    target = patch.status
    if target not in _LEGAL_JOB_TRANSITIONS[current]:
        raise ValueError(
            f"非法 job 狀態轉移:{current.value} → {target.value}"
        )
    job.status = target.value
    if patch.progress is not None:
        job.progress = patch.progress
    if patch.message is not None:
        job.message = patch.message
    if patch.error is not None:
        job.error = dict(patch.error)
    if patch.artifact_id is not None:
        job.artifact_id = patch.artifact_id
    if patch.result_metadata is not None:
        job.result_metadata = dict(patch.result_metadata)
    if patch.artifact_files is not None:
        job.artifact_files = list(patch.artifact_files)
    job.updated_at = _utcnow()
    db.commit()
    db.refresh(job)
    return job


# ── Artifact + Version ────────────────────────────────────────────────────────

def create_artifact(
    db: Session, *, payload: ArtifactIn, owner_user_id: int | None,
    source_task_id: int | None, source_snapshot_id: int | None,
    trace_id: str | None, initial_level: ClassificationLevel,
) -> Artifact:
    """建立 artifact 列(不含 version);binding 規則 fail-closed。

    binding(constitution §6):``source_task_id`` 或 ``source_snapshot_id``
    至少一,否則 ``ValueError``(router → 422)。``initial_level`` 先落
    explicit 宣告值,繼承升級由 orchestrator 走 policy 單向閂鎖(才會寫
    ClassificationEvent);故這裡不可預先灌 effective,否則閂鎖成 no-op、
    事件遺失。
    """
    if source_task_id is None and source_snapshot_id is None:
        raise ValueError(
            "artifact 必須綁 task 或 source_snapshot(constitution §6);"
            "兩者皆缺,拒絕落地"
        )
    artifact = Artifact(
        artifact_type=payload.artifact_type.value,
        title=payload.title,
        status="completed",
        owner_user_id=owner_user_id,
        source_task_id=source_task_id,
        source_snapshot_id=source_snapshot_id,
        job_id=payload.job_id,
        trace_id=trace_id,
        metadata_json=dict(payload.metadata) if payload.metadata else None,
        classification_level=initial_level.to_storage(),
    )
    db.add(artifact)
    db.commit()
    db.refresh(artifact)
    return artifact


def create_version(
    db: Session, *, artifact: Artifact, storage_ref: str,
    content_hash: str | None, file_refs: list[str],
    citation_map: dict | None, generated_by_model_id: int | None,
    generated_by_agent_id: int | None, generated_by_studio_job_id: str | None,
    level: ClassificationLevel,
) -> ArtifactVersion:
    """新增一個版本(version = 現有最大 + 1),並回填 ``current_version``。"""
    last = (
        db.query(ArtifactVersion.version)
        .filter(ArtifactVersion.artifact_id == artifact.id)
        .order_by(ArtifactVersion.version.desc())
        .limit(1)
        .scalar()
    ) or 0
    version = ArtifactVersion(
        artifact_id=artifact.id,
        version=last + 1,
        storage_ref=storage_ref,
        content_hash=content_hash,
        file_refs=list(file_refs or []),
        citation_map=dict(citation_map) if citation_map else None,
        generated_by_model_id=generated_by_model_id,
        generated_by_agent_id=generated_by_agent_id,
        generated_by_studio_job_id=generated_by_studio_job_id,
        classification_level=level.to_storage(),
    )
    db.add(version)
    artifact.current_version = last + 1
    artifact.updated_at = _utcnow()
    db.commit()
    db.refresh(version)
    return version


def record_export(
    db: Session, *, artifact: Artifact, payload: ArtifactExportIn,
    exporter_user_id: int | None, exporter_employee_id: str | None,
    policy_decision_id: int | None, level: ClassificationLevel,
) -> ExportRecord:
    """落一筆(已核可的)匯出列;只在 policy allow 後由 orchestrator 呼叫。"""
    export = ExportRecord(
        artifact_id=artifact.id,
        artifact_version_id=payload.artifact_version_id,
        exporter_user_id=exporter_user_id,
        exporter_employee_id=exporter_employee_id,
        target_space=payload.target_space,
        target_classification_floor=(
            payload.target_classification_floor.value
            if payload.target_classification_floor is not None
            else None
        ),
        export_format=payload.export_format,
        policy_decision_id=policy_decision_id,
        decision="allow",
        trace_id=artifact.trace_id,
        classification_level=level.to_storage(),
    )
    db.add(export)
    db.commit()
    db.refresh(export)
    return export


def sync_version_level(
    db: Session, *, version: ArtifactVersion, level: ClassificationLevel
) -> None:
    """把版本分類同步為 artifact 閂鎖後的 effective 等級。"""
    if version.classification_level != level.to_storage():
        version.classification_level = level.to_storage()
        db.commit()
        db.refresh(version)


# ── 治理讀面(owner-scope)────────────────────────────────────────────────────

def get_artifact(db: Session, artifact_id: int) -> Artifact | None:
    return db.query(Artifact).filter(Artifact.id == artifact_id).first()


def _is_owner_of(db: Session, artifact: Artifact, viewer_user_id: int) -> bool:
    """viewer 是否為 artifact owner:直接 owner_user_id,或其綁定 task 的申請人。"""
    if artifact.owner_user_id == viewer_user_id:
        return True
    if artifact.source_task_id is not None:
        requester = db.query(Task.requester_user_id).filter(
            Task.id == artifact.source_task_id
        ).scalar()
        if requester == viewer_user_id:
            return True
    return False


def ensure_artifact_access(
    db: Session, *, artifact: Artifact, viewer_user_id: int, is_admin: bool
) -> None:
    """治理讀取權:owner(直接或經 task 申請人)或 admin tier;否則 403。"""
    if is_admin or _is_owner_of(db, artifact, viewer_user_id):
        return
    raise PermissionError(
        f"使用者 {viewer_user_id} 無權存取 artifact {artifact.id}"
    )


def _positive_ints(raw: object) -> list[int]:
    """把 JSON 清單或單一整數收成正整數。順序保留、重複去掉。"""
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            try:
                raw = int(text)
            except ValueError:
                return []
    if isinstance(raw, bool):
        return []
    if isinstance(raw, int):
        return [raw] if raw > 0 else []
    if not isinstance(raw, list):
        return []
    found: list[int] = []
    for item in raw:
        if isinstance(item, bool):
            continue
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in found:
            found.append(number)
    return found


def _union_collection_ids(
    *,
    job_collection_id: int | None,
    metadata: object,
    task_collection_ids: object,
    snapshot_collection_ids: object,
) -> list[int]:
    """一個產出可能從 job、metadata、task、snapshot 各自帶知識庫。"""
    found: list[int] = []

    def add(raw: object) -> None:
        for number in _positive_ints(raw):
            if number not in found:
                found.append(number)

    add(job_collection_id)
    if isinstance(metadata, dict):
        add(metadata.get("collection_id"))
        add(metadata.get("collection_ids"))
    add(task_collection_ids)
    add(snapshot_collection_ids)
    return found


def _artifact_ids_in_collection(db: Session, collection_id: int) -> set[int]:
    """這個知識庫底下的 artifact id。

    artifacts 表沒有 collection 欄。知識庫寫在 job、成品 metadata、
    綁定 task 的 selected_collection_ids，或 snapshot 的 collection_ids。
    """
    ids: set[int] = set()
    job_ids = [
        job_id
        for (job_id,) in db.query(ArtifactJob.job_id).filter(
            ArtifactJob.collection_id == collection_id
        )
    ]
    if job_ids:
        for (artifact_id,) in db.query(Artifact.id).filter(
            Artifact.job_id.in_(job_ids)
        ):
            ids.add(int(artifact_id))
        for (artifact_id,) in db.query(ArtifactJob.artifact_id).filter(
            ArtifactJob.job_id.in_(job_ids),
            ArtifactJob.artifact_id.isnot(None),
        ):
            ids.add(int(artifact_id))

    referenced_tasks = [
        task_id
        for (task_id,) in db.query(Artifact.source_task_id).filter(
            Artifact.source_task_id.isnot(None)
        ).distinct()
    ]
    if referenced_tasks:
        matched_tasks = [
            task_id
            for task_id, raw in db.query(
                Task.id, Task.selected_collection_ids
            ).filter(Task.id.in_(referenced_tasks))
            if collection_id in _positive_ints(raw)
        ]
        if matched_tasks:
            for (artifact_id,) in db.query(Artifact.id).filter(
                Artifact.source_task_id.in_(matched_tasks)
            ):
                ids.add(int(artifact_id))

    referenced_snaps = [
        snap_id
        for (snap_id,) in db.query(Artifact.source_snapshot_id).filter(
            Artifact.source_snapshot_id.isnot(None)
        ).distinct()
    ]
    if referenced_snaps:
        matched_snaps = [
            snap_id
            for snap_id, raw in db.query(
                SourceSnapshot.id, SourceSnapshot.collection_ids
            ).filter(SourceSnapshot.id.in_(referenced_snaps))
            if collection_id in _positive_ints(raw)
        ]
        if matched_snaps:
            for (artifact_id,) in db.query(Artifact.id).filter(
                Artifact.source_snapshot_id.in_(matched_snaps)
            ):
                ids.add(int(artifact_id))

    for artifact_id, metadata in db.query(
        Artifact.id, Artifact.metadata_json
    ).filter(Artifact.metadata_json.isnot(None)):
        if collection_id in _union_collection_ids(
            job_collection_id=None,
            metadata=metadata,
            task_collection_ids=None,
            snapshot_collection_ids=None,
        ):
            ids.add(int(artifact_id))
    return ids


def attach_collection_scope(db: Session, rows: list[Artifact]) -> list[Artifact]:
    """把知識庫 id 寫到實例上，給讀取契約的 collection_id 用。

    不是資料庫欄位。只活在這次回應裡。
    """
    if not rows:
        return rows
    job_keys = [row.job_id for row in rows if row.job_id]
    task_keys = [row.source_task_id for row in rows if row.source_task_id is not None]
    snap_keys = [
        row.source_snapshot_id for row in rows if row.source_snapshot_id is not None
    ]
    jobs = {
        job.job_id: job
        for job in (
            db.query(ArtifactJob).filter(ArtifactJob.job_id.in_(job_keys)).all()
            if job_keys else []
        )
    }
    tasks = {
        task.id: task
        for task in (
            db.query(Task).filter(Task.id.in_(task_keys)).all() if task_keys else []
        )
    }
    snaps = {
        snap.id: snap
        for snap in (
            db.query(SourceSnapshot).filter(SourceSnapshot.id.in_(snap_keys)).all()
            if snap_keys else []
        )
    }
    for row in rows:
        job = jobs.get(row.job_id) if row.job_id else None
        task = tasks.get(row.source_task_id) if row.source_task_id is not None else None
        snap = (
            snaps.get(row.source_snapshot_id)
            if row.source_snapshot_id is not None else None
        )
        collected = _union_collection_ids(
            job_collection_id=getattr(job, "collection_id", None),
            metadata=row.metadata_json,
            task_collection_ids=getattr(task, "selected_collection_ids", None),
            snapshot_collection_ids=getattr(snap, "collection_ids", None),
        )
        setattr(row, "collection_ids", collected)
        setattr(row, "collection_id", collected[0] if collected else None)
    return rows


def list_artifacts(
    db: Session, *, viewer_user_id: int, is_admin: bool,
    artifact_type: str | None = None, task_id: int | None = None,
    classification_level: str | None = None,
    collection_id: int | None = None,
    limit: int = 50, offset: int = 0,
) -> list[Artifact]:
    """列出 artifacts(admin: 全部;一般使用者: 自己 owner 或經 task 申請人)。

    filters:``artifact_type`` / ``task_id`` / ``classification_level`` /
    ``collection_id``(知識庫，由 job／task／snapshot／metadata 彙出)。
    非 admin 的 owner-scope 用 (owner_user_id == viewer) OR (綁定 task 的
    requester == viewer) 兩路 union。
    """
    q = db.query(Artifact)
    if not is_admin:
        owned_task_ids = (
            select(Task.id)
            .where(Task.requester_user_id == viewer_user_id)
            .scalar_subquery()
        )
        q = q.filter(
            or_(
                Artifact.owner_user_id == viewer_user_id,
                Artifact.source_task_id.in_(owned_task_ids),
            )
        )
    if artifact_type is not None:
        q = q.filter(Artifact.artifact_type == artifact_type)
    if task_id is not None:
        q = q.filter(Artifact.source_task_id == task_id)
    if classification_level is not None:
        q = q.filter(Artifact.classification_level == classification_level)
    if collection_id is not None:
        matched = _artifact_ids_in_collection(db, collection_id)
        if not matched:
            return []
        q = q.filter(Artifact.id.in_(matched))
    rows = (
        q.order_by(Artifact.created_at.desc(), Artifact.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return attach_collection_scope(db, rows)
