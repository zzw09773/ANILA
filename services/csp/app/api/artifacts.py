# -*- coding: utf-8 -*-
"""Artifact REST surface (Slice 8a — CSP artifact contract).

doc 02(ArtifactJob 逐欄、§8「Studio restart job 不丟失」)、doc 09 §Artifact
API、doc 01(Artifact/Version/Export、binding 規則)、doc 08(§5 四共通分類、
§10 匯出判定)、doc 10 §12 邊界(Studio 不直讀 CSP DB → 經 HTTP + service
token 回報)。

本檔是 **orchestrator**:協調三個獨立面 —— artifacts module(DB 落地、
binding、讀取)、policy 核心(單向分類閂鎖 + PolicyDecision + 匯出 gate)、
tasks/snapshot 來源等級。因此放在 ``app.api``(api → modules 單向合法),
而非放進 ``app.modules.artifacts``(該 module 受 independence 契約約束,
不得 import policy/tasks)。

路由(和 ``traces.py`` 同樣「不帶 APIRouter prefix、寫完整路徑」掛載,
nginx ``/v1`` 直通吃得到 service 面):

- ``POST /v1/artifact-jobs``(service token)—— 冪等 upsert(job_id)。
- ``PATCH /v1/artifact-jobs/{job_id}``(service token)—— 狀態轉移 + 進度/回填。
- ``POST /v1/artifacts``(service token)—— binding 驗證 + 分類繼承(effective
  = max(explicit, task, snapshot))+ 建 artifact 首版。
- ``POST /v1/artifacts/{artifact_id}/versions``(service token)—— 新版 + 繼承
  重驗。
- ``POST /v1/artifacts/{artifact_id}/exports``(user JWT 或 service token)——
  匯出 policy gate(allow if target_floor >= artifact.level;deny → 403 + deny
  PolicyDecision、不落 allow 匯出列)。
- ``GET /api/artifacts`` + ``GET /api/artifacts/{id}``(user JWT)—— 治理讀面。

服務面(``/v1`` 寫)僅接受 service token(doc 10 §12):使用者 JWT → 403
(artifact/job 由 Studio 服務註冊,不開放使用者直建);匿名 → 401。
"""

from __future__ import annotations

import json
import logging
import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.middleware.caller import ACCESS_COOKIE_NAME, _extract_bearer, get_caller
from app.models.artifact import Artifact, ArtifactJob, ArtifactVersion
from app.models.audit_log import AuditLog
from app.models.ingestion import IngestionDocument
from app.models.registered_service import RegisteredService
from app.models.source_snapshot import SourceSnapshot
from app.models.task import Task, TaskRun
from app.models.user import User
from app.modules import artifacts, policy, tasks
from app.modules.artifacts.blob_store import (
    BlobValidationError,
    remove_blob,
    resolve_blob_path,
    store_stream,
)
from app.modules.clearance.service import (
    ClearancePolicyDataError,
    resolve_and_evaluate_data_access,
)
from app.schemas.contracts.artifacts import (
    ArtifactDetailOut,
    ArtifactExportIn,
    ArtifactIn,
    ArtifactJobIn,
    ArtifactJobLeaseIn,
    ArtifactJobOut,
    ArtifactJobPatch,
    ArtifactLegalHoldIn,
    ArtifactOut,
    ArtifactRegisterResult,
    ArtifactUploadMetadata,
    ArtifactVersionIn,
    ArtifactVersionRevokeIn,
    ArtifactVersionResult,
    ExportResult,
)
from anila_contracts import Classification as ClassificationLevel
from app.services import agent_credential_service
from app.services.auth_service import get_current_user, require_admin

logger = logging.getLogger(__name__)

def _gate2_pilot_artifact_gate() -> None:
    if settings.ANILA_PILOT_MODE and not settings.ENABLE_PILOT_STUDIO_ARTIFACTS:
        raise HTTPException(status_code=404, detail="Gate 2 pilot 未啟用 Artifact")


router = APIRouter(
    tags=["Artifact"], dependencies=[Depends(_gate2_pilot_artifact_gate)]
)

_INHERIT_REASON = "source_selected"


# ── 認證依賴 ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _ServiceCaller:
    """Artifact writer with an explicit registry identity and capability."""

    identity: agent_credential_service.CallerIdentity
    service: RegisteredService

    @property
    def actor_id(self) -> str:
        return f"service_client:{self.identity.service_client_id}"


def _artifact_service_for_identity(
    db: Session, identity: agent_credential_service.CallerIdentity
) -> RegisteredService:
    """Resolve the one active artifact-capable service bound to this client."""
    if (
        identity.kind != "service_client"
        or identity.service_client_id is None
        or identity.is_legacy
    ):
        raise HTTPException(
            status_code=403,
            detail="Artifact 寫入需使用具名、非 legacy 的 Service Client 憑證",
        )
    candidates = (
        db.query(RegisteredService)
        .filter(
            RegisteredService.service_client_id == identity.service_client_id,
            RegisteredService.is_active.is_(True),
        )
        .all()
    )
    services = [
        row
        for row in candidates
        if row.service_type == "artifact_tool"
        and set(row.data_egress or []) == {"artifact"}
    ]
    if len(candidates) != 1 or len(services) != 1:
        raise HTTPException(
            status_code=403,
            detail="Service Client 未唯一綁定精確 artifact_tool + artifact egress 能力",
        )
    return services[0]


def _resolve_service_token(request: Request, db: Session):
    """回 (matched: bool, identity)。matched=False 代表無有效 service token。"""
    token = request.headers.get("X-CSP-Service-Token")
    if not token:
        return False, None
    identity = agent_credential_service.verify_service_token(db, token=token)
    if identity is not None:
        request.state.csp_caller = identity
        return True, identity
    # header present but invalid → 明確 401(不是 403)。
    raise HTTPException(status_code=401, detail="服務權杖無效")


def require_service_caller(
    request: Request, db: Session = Depends(get_db)
) -> _ServiceCaller:
    """/v1 寫入面 gate:僅 service token(doc 10 §12)。

    無 service token 但帶使用者憑證(JWT/cookie)→ 403(此端點不開放使用者
    直建 artifact/job);完全匿名 → 401。
    """
    matched, identity = _resolve_service_token(request, db)
    if matched and identity is not None:
        return _ServiceCaller(
            identity=identity,
            service=_artifact_service_for_identity(db, identity),
        )
    has_user_cred = bool(
        _extract_bearer(request.headers.get("Authorization"))
        or request.cookies.get(ACCESS_COOKIE_NAME)
    )
    if has_user_cred:
        raise HTTPException(
            status_code=403,
            detail="此端點僅接受服務憑證(X-CSP-Service-Token),不開放使用者直建",
        )
    raise HTTPException(
        status_code=401, detail="缺少 X-CSP-Service-Token(服務對服務端點)"
    )


@dataclass(frozen=True)
class _ExportCaller:
    """匯出面呼叫者:user JWT 或 service token。"""

    actor_type: str
    actor_id: str
    exporter_user_id: int | None
    exporter_employee_id: str | None
    service_id: int | None = None
    service_slug: str | None = None


def require_export_caller(
    request: Request, db: Session = Depends(get_db)
) -> _ExportCaller:
    """匯出 gate 接受 user JWT 或 service token(doc 09 Artifact export)。"""
    matched, identity = _resolve_service_token(request, db)
    if matched and identity is not None:
        service = _artifact_service_for_identity(db, identity)
        actor_id = f"service_client:{identity.service_client_id}"
        return _ExportCaller(
            actor_type="service", actor_id=actor_id,
            exporter_user_id=None, exporter_employee_id=None,
            service_id=service.id, service_slug=service.slug,
        )
    # 落到使用者 JWT / sk- API key / cookie(匿名由 get_caller fail-closed 401)。
    user = get_caller(request, db).user
    return _ExportCaller(
        actor_type="user", actor_id=str(user.id),
        exporter_user_id=user.id, exporter_employee_id=user.username,
    )


# ── 內部 helpers ─────────────────────────────────────────────────────────────


def _load_artifact_or_404(db: Session, artifact_id: int) -> Artifact:
    artifact = artifacts.get_artifact(db, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="找不到此 artifact")
    return artifact


def _resolve_binding(
    db: Session, *, task_id: int | None, source_snapshot_id: int | None
) -> tuple[Task | None, SourceSnapshot | None]:
    """存在性驗證:給了 id 就必須查得到列,否則 404。"""
    task = None
    snapshot = None
    if task_id is not None:
        task = (
            db.query(Task)
            .filter(Task.id == task_id)
            .with_for_update()
            .first()
        )
        if task is None:
            raise HTTPException(status_code=404, detail=f"找不到 task {task_id}")
    if source_snapshot_id is not None:
        snapshot = db.query(SourceSnapshot).filter(
            SourceSnapshot.id == source_snapshot_id
        ).first()
        if snapshot is None:
            raise HTTPException(
                status_code=404,
                detail=f"找不到 source_snapshot {source_snapshot_id}",
            )
        if task is None:
            task = (
                db.query(Task)
                .filter(Task.id == snapshot.task_id)
                .with_for_update()
                .first()
            )
            if task is None:
                raise HTTPException(
                    status_code=409,
                    detail="source_snapshot 已失去所屬 Task，拒絕註冊 artifact",
                )
        elif snapshot.task_id != task.id:
            raise HTTPException(
                status_code=403,
                detail="source_snapshot 與 Task ownership 不一致",
            )
    return task, snapshot


def _require_task_service_and_run(
    db: Session, *, task: Task, caller: _ServiceCaller
) -> TaskRun:
    selected = (task.selected_service_id or "").strip()
    if selected not in {str(caller.service.id), caller.service.slug}:
        raise HTTPException(
            status_code=403,
            detail="Task 未指派給目前的 Artifact Service",
        )
    if task.status != "running":
        raise HTTPException(
            status_code=409,
            detail=f"Task 狀態 {task.status!r} 不可註冊完成 artifact",
        )
    run = (
        db.query(TaskRun)
        .filter(
            TaskRun.task_id == task.id,
            TaskRun.dispatch_target == "studio",
            TaskRun.status == "running",
        )
        .with_for_update()
        .one_or_none()
    )
    if run is None:
        raise HTTPException(
            status_code=409,
            detail="Task 缺少目前 Artifact Service 的 active studio TaskRun",
        )
    return run


def _service_audit_log(
    *, caller: _ServiceCaller, action: str, resource_id: str, detail: str
) -> AuditLog:
    return AuditLog(
        actor_user_id=None,
        actor_username=caller.actor_id,
        action=action,
        resource_type="artifact",
        resource_id=resource_id,
        status="success",
        detail=detail,
        metadata_json=json.dumps(
            {
                "actor_type": "service",
                "service_client_id": caller.identity.service_client_id,
                "registered_service_id": caller.service.id,
                "registered_service_slug": caller.service.slug,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )


def _latch_inheritance(
    db: Session, *, artifact: Artifact, task: Task | None,
    snapshot: SourceSnapshot | None, actor_id: str,
    explicit_floor: ClassificationLevel | None = None,
    commit: bool = True,
) -> ClassificationLevel:
    """把 artifact 分類單向閂鎖到 max(current, task, snapshot, explicit_floor)。

    走 policy 核心(唯一會寫 ClassificationEvent 的路徑);回閂鎖後等級。
    """
    target = artifacts.inherited_level(
        db,
        source_task_id=task.id if task is not None else None,
        source_snapshot_id=snapshot.id if snapshot is not None else None,
    )
    candidates = [c for c in (target, explicit_floor) if c is not None]
    if not candidates:
        return ClassificationLevel.from_storage(artifact.classification_level)
    new_level = ClassificationLevel.max_of(candidates)
    policy.apply_classification(
        db,
        resource_type="artifact",
        resource_id=str(artifact.id),
        new_level=new_level.to_storage(),
        actor_type="service",
        actor_id=actor_id,
        reason=_INHERIT_REASON,
        task_id=task.id if task is not None else None,
        source="artifact_inheritance",
        commit=commit,
    )
    db.refresh(artifact)
    return ClassificationLevel.from_storage(artifact.classification_level)


def _parse_upload_metadata(raw: str) -> ArtifactUploadMetadata:
    try:
        return ArtifactUploadMetadata.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc


def _upload_metadata_digest(payload: ArtifactUploadMetadata) -> str:
    canonical = json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _existing_upload_result(
    db: Session,
    *,
    job: ArtifactJob,
    payload: ArtifactUploadMetadata,
    metadata_digest: str,
) -> ArtifactRegisterResult | None:
    """Return an exact replay; any partial/mismatched authority fails closed."""
    if job.artifact_id is None:
        existing = db.query(Artifact).filter(Artifact.job_id == job.job_id).one_or_none()
        if existing is None:
            if job.artifact_upload_sha256 or job.artifact_upload_metadata_digest:
                raise HTTPException(status_code=409, detail="artifact upload checkpoint 不完整")
            return None
        raise HTTPException(status_code=409, detail="artifact/job authority 不一致")
    if (
        job.artifact_upload_sha256 != payload.content_sha256
        or job.artifact_upload_metadata_digest != metadata_digest
    ):
        raise HTTPException(status_code=409, detail="job_id 已綁定不同 artifact payload")
    artifact = db.get(Artifact, job.artifact_id)
    if (
        artifact is None
        or artifact.job_id != job.job_id
        or artifact.artifact_type != payload.artifact_type.value
        or artifact.title != payload.title
        or artifact.source_task_id != payload.task_id
        or artifact.source_snapshot_id != payload.source_snapshot_id
    ):
        raise HTTPException(status_code=409, detail="artifact replay authority 已損壞")
    version = (
        db.query(ArtifactVersion)
        .filter(
            ArtifactVersion.artifact_id == artifact.id,
            ArtifactVersion.version == artifact.current_version,
        )
        .one_or_none()
    )
    if (
        version is None
        or version.content_hash != payload.content_sha256
        or version.blob_size_bytes != payload.content_size
        or version.media_type != payload.media_type
        or version.original_filename != payload.original_filename
        or version.lifecycle_state == "erased"
    ):
        raise HTTPException(status_code=409, detail="artifact replay version 不一致")
    return ArtifactRegisterResult(
        artifact_id=artifact.id,
        version_id=version.id,
        classification_level=ClassificationLevel.from_storage(
            artifact.classification_level
        ),
        download_url=f"/api/artifacts/{artifact.id}/versions/{version.id}/download",
    )


def _artifact_download_authorized(
    db: Session,
    *,
    artifact: Artifact,
    version: ArtifactVersion,
    user: User,
) -> None:
    """Fail closed across owner, immutable provenance, and data clearance."""
    if artifact.owner_user_id != user.id:
        raise HTTPException(status_code=403, detail="artifact owner 不符")
    if artifact.source_task_id is None or artifact.source_snapshot_id is None:
        raise HTTPException(status_code=403, detail="artifact 缺少完整 Task/Snapshot binding")
    task = db.get(Task, artifact.source_task_id)
    snapshot = db.get(SourceSnapshot, artifact.source_snapshot_id)
    if (
        task is None
        or snapshot is None
        or task.requester_user_id != user.id
        or snapshot.task_id != task.id
        or task.source_snapshot_id != snapshot.id
    ):
        raise HTTPException(status_code=403, detail="Task/Snapshot ownership 不符")

    try:
        required_level = ClassificationLevel.max_of(
            [
                ClassificationLevel.from_storage(artifact.classification_level),
                ClassificationLevel.from_storage(version.classification_level),
                ClassificationLevel.from_storage(task.classification_level),
                ClassificationLevel.from_storage(snapshot.classification_level),
            ]
        )
        collection_ids = {int(value) for value in (snapshot.collection_ids or [])}
        selected_ids = {int(value) for value in (task.selected_collection_ids or [])}
        document_ids = {int(value) for value in (snapshot.document_ids or [])}
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="artifact provenance 資料無效") from exc
    if not collection_ids or not collection_ids.issubset(selected_ids):
        raise HTTPException(status_code=403, detail="Snapshot collection scope 不符")

    docs_by_collection: dict[int, list[int]] = {item: [] for item in collection_ids}
    if document_ids:
        documents = (
            db.query(IngestionDocument)
            .filter(IngestionDocument.id.in_(document_ids))
            .all()
        )
        if len(documents) != len(document_ids):
            raise HTTPException(status_code=403, detail="Snapshot document 已失效")
        for document in documents:
            if document.collection_id not in collection_ids:
                raise HTTPException(status_code=403, detail="Snapshot document 越界")
            docs_by_collection[document.collection_id].append(document.id)

    try:
        for collection_id, doc_ids in docs_by_collection.items():
            targets = doc_ids or [None]
            for document_id in targets:
                decision = resolve_and_evaluate_data_access(
                    db,
                    user_id=user.id,
                    collection_id=collection_id,
                    document_id=document_id,
                )
                if (
                    not decision.allowed
                    or decision.authorized_classification is None
                    or decision.authorized_classification < required_level
                ):
                    raise HTTPException(status_code=403, detail="artifact clearance/compartment 拒絕")
    except (LookupError, ClearancePolicyDataError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="artifact clearance 資料無效") from exc


# ── /v1 service 面 ────────────────────────────────────────────────────────────


@router.get("/v1/artifact-writer/ready")
def artifact_writer_ready(
    caller: _ServiceCaller = Depends(require_service_caller),
):
    """Side-effect-free credential/capability probe for Studio readiness."""
    return {
        "ready": True,
        "service_id": caller.service.id,
        "service_slug": caller.service.slug,
        "service_type": caller.service.service_type,
        "data_egress": list(caller.service.data_egress or []),
    }


@router.post("/v1/artifact-jobs", response_model=ArtifactJobOut, status_code=201)
def register_artifact_job(
    payload: ArtifactJobIn,
    caller: _ServiceCaller = Depends(require_service_caller),
    db: Session = Depends(get_db),
):
    """冪等 upsert 一筆 Studio job(doc 02 ArtifactJob;restart 不丟失)。"""
    _require_job_selected_service(
        db, task_id=payload.task_id, caller=caller,
    )
    owner_user_id, employee_id = artifacts.resolve_owner(
        db, requester_user_id=payload.requester_user_id,
        employee_id=payload.employee_id,
    )
    try:
        return artifacts.register_job(
            db, payload=payload, owner_user_id=owner_user_id,
            employee_id=employee_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.get("/v1/artifact-jobs/{job_id}/authority")
def read_artifact_job_authority(
    job_id: str,
    caller: _ServiceCaller = Depends(require_service_caller),
    db: Session = Depends(get_db),
):
    """Return the committed CSP authority used for restart convergence."""
    job = db.get(ArtifactJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="找不到此 job")
    _require_job_selected_service(db, task_id=job.task_id, caller=caller)
    body = {
        "job_id": job.job_id,
        "status": job.status,
        "artifact_id": job.artifact_id,
        "current_version": None,
        "classification_level": None,
        "content_hash": None,
        "download_url": None,
    }
    if job.artifact_id is None:
        return body
    artifact = db.get(Artifact, job.artifact_id)
    if artifact is None or artifact.job_id != job.job_id:
        raise HTTPException(status_code=409, detail="artifact/job authority 不一致")
    version = (
        db.query(ArtifactVersion)
        .filter(
            ArtifactVersion.artifact_id == artifact.id,
            ArtifactVersion.version == artifact.current_version,
        )
        .one_or_none()
    )
    if version is None or not version.is_active or version.lifecycle_state != "active":
        raise HTTPException(status_code=409, detail="artifact current version 不可下載")
    body.update(
        {
            "current_version": artifact.current_version,
            "classification_level": artifact.classification_level,
            "content_hash": version.content_hash,
            "download_url": (
                f"/api/artifacts/{artifact.id}/versions/{version.id}/download"
            ),
        }
    )
    return body


@router.put("/v1/artifact-jobs/{job_id}/lease", response_model=ArtifactJobOut)
def bind_artifact_job_lease(
    job_id: str,
    payload: ArtifactJobLeaseIn,
    caller: _ServiceCaller = Depends(require_service_caller),
    db: Session = Depends(get_db),
):
    """Bind/refresh the current durable Redis attempt for sink admission."""
    job = db.query(ArtifactJob).filter(ArtifactJob.job_id == job_id).with_for_update().one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="找不到此 job")
    _require_job_selected_service(
        db, task_id=job.task_id, caller=caller, lock=True,
    )
    if job.status not in {"queued", "running"} or job.artifact_id is not None:
        raise HTTPException(status_code=409, detail="終態 job 不可刷新 lease")
    if job.durable_attempt is not None and payload.attempt < job.durable_attempt:
        raise HTTPException(status_code=409, detail="durable attempt 不可倒退")
    incoming_digest = hashlib.sha256(payload.lease_token.encode()).hexdigest()
    if (
        job.durable_attempt == payload.attempt
        and job.durable_lease_digest is not None
        and not hmac.compare_digest(job.durable_lease_digest, incoming_digest)
    ):
        raise HTTPException(status_code=409, detail="相同 durable attempt 的 lease 不可替換")
    job.durable_attempt = payload.attempt
    job.durable_lease_digest = incoming_digest
    job.expires_at = datetime.now(timezone.utc) + timedelta(seconds=payload.lease_seconds)
    db.commit()
    db.refresh(job)
    return job


@router.patch(
    "/v1/artifact-jobs/{job_id}", response_model=ArtifactJobOut
)
def patch_artifact_job(
    job_id: str,
    patch: ArtifactJobPatch,
    caller: _ServiceCaller = Depends(require_service_caller),
    db: Session = Depends(get_db),
):
    """狀態轉移 + 進度/回填;非法轉移 → 409、查無 → 404。"""
    try:
        current = (
            db.query(ArtifactJob)
            .filter(ArtifactJob.job_id == job_id)
            .with_for_update()
            .one_or_none()
        )
        if current is None:
            raise LookupError(job_id)
        task = _require_job_selected_service(
            db, task_id=current.task_id, caller=caller, lock=True,
        )
        active_studio_run: TaskRun | None = None
        if patch.status.value in {"failed", "cancelled"} and current.task_id is not None:
            assert task is not None
            active_runs = (
                db.query(TaskRun)
                .filter(
                    TaskRun.task_id == current.task_id,
                    TaskRun.status.in_(("queued", "running")),
                )
                .with_for_update()
                .all()
            )
            if len(active_runs) > 1:
                raise ValueError("Task 存在多個 active TaskRun")
            if current.status not in {"failed", "cancelled"}:
                if not active_runs or active_runs[0].dispatch_target != "studio":
                    raise ValueError(
                        "ArtifactJob terminal closure 缺少 active studio TaskRun"
                    )
                active_studio_run = active_runs[0]
        job = artifacts.transition_job(db, job_id=job_id, patch=patch, commit=False)
        if active_studio_run is not None:
            tasks.finish_task_run(
                db,
                task_run=active_studio_run,
                status=patch.status.value,
                error=patch.error,
                commit=False,
            )
        db.commit()
        db.refresh(job)
        return job
    except LookupError:
        db.rollback()
        raise HTTPException(status_code=404, detail="找不到此 job") from None
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post(
    "/v1/artifacts/upload", response_model=ArtifactRegisterResult, status_code=201
)
def upload_artifact(
    metadata_json: str = Form(...),
    file: UploadFile = File(...),
    studio_attempt: str | None = Header(None, alias="X-Studio-Attempt"),
    studio_lease_token: str | None = Header(None, alias="X-Studio-Lease-Token"),
    caller: _ServiceCaller = Depends(require_service_caller),
    db: Session = Depends(get_db),
):
    """Create one authoritative Artifact/Version from verified immutable bytes."""
    payload = _parse_upload_metadata(metadata_json)
    job = (
        db.query(ArtifactJob)
        .filter(ArtifactJob.job_id == payload.job_id)
        .with_for_update()
        .one_or_none()
        if payload.job_id else None
    )
    if job is None:
        raise HTTPException(status_code=404, detail="immutable artifact 必須綁已存在的 job")
    try:
        attempt = int((studio_attempt or "").strip())
    except ValueError:
        attempt = 0
    lease_token = (studio_lease_token or "").strip()
    expires_at = job.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    lease_digest = hashlib.sha256(lease_token.encode()).hexdigest()
    if (
        attempt < 1
        or len(lease_token) < 16
        or job.durable_attempt != attempt
        or not job.durable_lease_digest
        or not hmac.compare_digest(job.durable_lease_digest, lease_digest)
        or expires_at is None
        or expires_at <= datetime.now(timezone.utc)
    ):
        raise HTTPException(status_code=409, detail="artifact upload durable lease 已失效")
    if job.task_id != payload.task_id or job.source_snapshot_id != payload.source_snapshot_id:
        raise HTTPException(status_code=403, detail="artifact job/Task/Snapshot immutable binding 不一致")
    task, snapshot = _resolve_binding(
        db,
        task_id=payload.task_id,
        source_snapshot_id=payload.source_snapshot_id,
    )
    if task is None or snapshot is None:
        raise HTTPException(status_code=422, detail="immutable artifact 必須同時綁 Task/Snapshot")
    if task.source_snapshot_id != snapshot.id:
        raise HTTPException(status_code=403, detail="Task canonical SourceSnapshot 不符")
    if (task.selected_service_id or "").strip() not in {
        str(caller.service.id), caller.service.slug
    }:
        raise HTTPException(status_code=403, detail="Task 未指派給目前的 Artifact Service")
    if payload.generated_by_agent_id is not None:
        raise HTTPException(status_code=403, detail="Service Client 不可冒用 Agent 身分")
    snapshot_collections = {int(value) for value in (snapshot.collection_ids or [])}
    if (
        job.owner_user_id != task.requester_user_id
        or job.task_id != task.id
        or job.source_snapshot_id != snapshot.id
        or (job.collection_id is not None and job.collection_id not in snapshot_collections)
    ):
        raise HTTPException(status_code=403, detail="artifact job/Task/Snapshot ownership 不一致")
    if file.content_type and file.content_type != payload.media_type:
        raise HTTPException(status_code=422, detail="multipart Content-Type 與 metadata MIME 不符")

    metadata_digest = _upload_metadata_digest(payload)
    replay = _existing_upload_result(
        db, job=job, payload=payload, metadata_digest=metadata_digest
    )
    if replay is not None:
        return replay
    task_run = _require_task_service_and_run(db, task=task, caller=caller)

    try:
        stored = store_stream(
            file.file,
            storage_root=settings.ARTIFACT_BLOB_STORAGE_PATH,
            artifact_type=payload.artifact_type.value,
            declared_sha256=payload.content_sha256,
            declared_size=payload.content_size,
            media_type=payload.media_type,
            original_filename=payload.original_filename,
        )
    except (BlobValidationError, FileExistsError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    committed = False
    try:
        explicit = payload.classification_level or ClassificationLevel.UNCLASSIFIED
        artifact = artifacts.create_artifact(
            db,
            payload=payload,
            owner_user_id=task.requester_user_id,
            source_task_id=task.id,
            source_snapshot_id=snapshot.id,
            trace_id=task.trace_id,
            initial_level=explicit,
            commit=False,
        )
        effective = _latch_inheritance(
            db,
            artifact=artifact,
            task=task,
            snapshot=snapshot,
            actor_id=caller.actor_id,
            commit=False,
        )
        version = artifacts.create_version(
            db,
            artifact=artifact,
            storage_ref=None,
            content_hash=stored.sha256,
            file_refs=[],
            citation_map=payload.citation_map,
            generated_by_model_id=payload.generated_by_model_id,
            generated_by_agent_id=None,
            generated_by_studio_job_id=payload.job_id,
            level=effective,
            blob_key=stored.key,
            blob_size_bytes=stored.size_bytes,
            media_type=stored.media_type,
            original_filename=stored.original_filename,
            commit=False,
        )
        version.archive_due_at = datetime.now(timezone.utc) + timedelta(
            days=settings.RETENTION_ARTIFACT_ACTIVE_DAYS
        )
        artifact.active_version_count = 1
        artifact.erased_version_count = 0
        tasks.finish_task_run(db, task_run=task_run, status="completed", commit=False)
        job.artifact_id = artifact.id
        job.artifact_upload_sha256 = stored.sha256
        job.artifact_upload_metadata_digest = metadata_digest
        db.add(
            _service_audit_log(
                caller=caller,
                action="artifact.uploaded",
                resource_id=str(artifact.id),
                detail=f"version={version.version}; sha256={stored.sha256}; bytes={stored.size_bytes}",
            )
        )
        db.flush()
        db.commit()
        committed = True
        return ArtifactRegisterResult(
            artifact_id=artifact.id,
            version_id=version.id,
            classification_level=effective,
            download_url=f"/api/artifacts/{artifact.id}/versions/{version.id}/download",
        )
    except Exception:
        db.rollback()
        raise
    finally:
        if not committed:
            remove_blob(settings.ARTIFACT_BLOB_STORAGE_PATH, stored.key)


@router.post(
    "/v1/artifacts", response_model=ArtifactRegisterResult, status_code=201
)
def register_artifact(
    payload: ArtifactIn,
    caller: _ServiceCaller = Depends(require_service_caller),
    db: Session = Depends(get_db),
):
    """註冊成品 + 首版;binding 驗證 + 分類繼承(effective = max)。"""
    raise HTTPException(
        status_code=410,
        detail="client storage_ref 不再是 authority;請改用 /v1/artifacts/upload",
    )
    task, snapshot = _resolve_binding(
        db, task_id=payload.task_id,
        source_snapshot_id=payload.source_snapshot_id,
    )
    if payload.task_id is None and payload.source_snapshot_id is None:
        raise HTTPException(
            status_code=422,
            detail="artifact 必須綁 task 或 source_snapshot",
        )
    if task is None:
        raise HTTPException(status_code=409, detail="artifact 必須能解析至所屬 Task")
    task_run = _require_task_service_and_run(db, task=task, caller=caller)
    if payload.generated_by_agent_id is not None:
        raise HTTPException(
            status_code=403,
            detail="Service Client 不可冒用 Agent 身分寫入 artifact provenance",
        )
    job = db.get(ArtifactJob, payload.job_id) if payload.job_id else None
    if payload.job_id and job is None:
        raise HTTPException(status_code=404, detail=f"找不到 artifact job {payload.job_id}")
    if job is not None and (
        job.owner_user_id != task.requester_user_id
        or (job.task_id is not None and job.task_id != task.id)
        or (
            job.source_snapshot_id is not None
            and job.source_snapshot_id != payload.source_snapshot_id
        )
    ):
        raise HTTPException(
            status_code=403,
            detail="artifact job 與 Task requester/ownership 不一致",
        )
    # owner / trace 由 task 優先、否則 job。
    owner_user_id: int | None = None
    trace_id: str | None = None
    owner_user_id = task.requester_user_id
    trace_id = task.trace_id

    explicit = payload.classification_level or ClassificationLevel.UNCLASSIFIED
    try:
        artifact = artifacts.create_artifact(
            db, payload=payload, owner_user_id=owner_user_id,
            source_task_id=payload.task_id,
            source_snapshot_id=payload.source_snapshot_id,
            trace_id=trace_id, initial_level=explicit, commit=False,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    effective = _latch_inheritance(
        db, artifact=artifact, task=task, snapshot=snapshot,
        actor_id=caller.actor_id, commit=False,
    )
    version = artifacts.create_version(
        db, artifact=artifact, storage_ref=payload.storage_ref,
        content_hash=payload.content_hash, file_refs=payload.file_refs,
        citation_map=payload.citation_map,
        generated_by_model_id=payload.generated_by_model_id,
        generated_by_agent_id=payload.generated_by_agent_id,
        generated_by_studio_job_id=payload.job_id, level=effective,
        commit=False,
    )
    tasks.finish_task_run(
        db,
        task_run=task_run,
        status="completed",
        commit=False,
    )
    if job is not None:
        job.artifact_id = artifact.id
    db.add(_service_audit_log(
        caller=caller,
        action="artifact.registered",
        resource_id=str(artifact.id),
        detail=f"version={version.version}; task={task.id}",
    ))
    db.flush()
    db.commit()
    return ArtifactRegisterResult(
        artifact_id=artifact.id, version_id=version.id,
        classification_level=effective,
    )


@router.post(
    "/v1/artifacts/{artifact_id}/versions",
    response_model=ArtifactVersionResult, status_code=201,
)
def add_artifact_version(
    artifact_id: int,
    payload: ArtifactVersionIn,
    caller: _ServiceCaller = Depends(require_service_caller),
    db: Session = Depends(get_db),
):
    """新增版本;繼承重驗(可再升不可降)。"""
    raise HTTPException(
        status_code=410,
        detail="client storage_ref 不再是 authority;版本必須走 immutable upload",
    )
    artifact = _load_artifact_or_404(db, artifact_id)
    task, snapshot = _resolve_binding(
        db, task_id=artifact.source_task_id,
        source_snapshot_id=artifact.source_snapshot_id,
    )
    if task is None:
        raise HTTPException(status_code=409, detail="artifact 已失去所屬 Task")
    selected = (task.selected_service_id or "").strip()
    if selected not in {str(caller.service.id), caller.service.slug}:
        raise HTTPException(status_code=403, detail="Task 未指派給目前的 Artifact Service")
    if payload.generated_by_agent_id is not None:
        raise HTTPException(
            status_code=403,
            detail="Service Client 不可冒用 Agent 身分寫入 artifact provenance",
        )
    effective = _latch_inheritance(
        db, artifact=artifact, task=task, snapshot=snapshot,
        actor_id=caller.actor_id,
        explicit_floor=payload.classification_level, commit=False,
    )
    version = artifacts.create_version(
        db, artifact=artifact, storage_ref=payload.storage_ref,
        content_hash=payload.content_hash, file_refs=payload.file_refs,
        citation_map=payload.citation_map,
        generated_by_model_id=payload.generated_by_model_id,
        generated_by_agent_id=payload.generated_by_agent_id,
        generated_by_studio_job_id=payload.generated_by_studio_job_id,
        level=effective, commit=False,
    )
    db.add(_service_audit_log(
        caller=caller,
        action="artifact.version.registered",
        resource_id=str(artifact.id),
        detail=f"version={version.version}",
    ))
    db.flush()
    db.commit()
    return ArtifactVersionResult(
        artifact_id=artifact.id, version_id=version.id,
        version=version.version, classification_level=effective,
    )


@router.post(
    "/v1/artifacts/{artifact_id}/exports",
    response_model=ExportResult, status_code=201,
)
def export_artifact(
    artifact_id: int,
    payload: ArtifactExportIn,
    caller: _ExportCaller = Depends(require_export_caller),
    db: Session = Depends(get_db),
):
    """匯出 policy gate(doc 08 §10:allow if target_floor >= artifact.level)。

    deny → 403 + 一筆 deny PolicyDecision,**不落** allow 匯出列
    (doc 00 §6:未通過 classification policy 的資料匯出 frozen)。
    """
    artifact = _load_artifact_or_404(db, artifact_id)
    if caller.actor_type == "user":
        assert caller.exporter_user_id is not None
        try:
            artifacts.ensure_artifact_access(
                db,
                artifact=artifact,
                viewer_user_id=caller.exporter_user_id,
            )
        except PermissionError:
            # Authorization must precede policy/audit/export writes and any
            # source Task mutation.  Otherwise an authenticated foreign user
            # could use the classification deny branch to mutate another
            # owner's Task even though they cannot read the artifact.
            raise HTTPException(
                status_code=403, detail="無權匯出此 artifact"
            ) from None
    else:
        task = db.get(Task, artifact.source_task_id) if artifact.source_task_id else None
        selected = (task.selected_service_id or "").strip() if task is not None else ""
        if selected not in {
            str(caller.service_id),
            caller.service_slug or "",
        }:
            raise HTTPException(
                status_code=403,
                detail="Artifact 匯出與 Task 指派服務不符",
            )
    artifact_level = ClassificationLevel.from_storage(artifact.classification_level)
    target_floor = payload.target_classification_floor
    allowed = target_floor >= artifact_level
    decision = "allow" if allowed else "deny"
    reason = (
        f"匯出判定 target_floor={target_floor.to_storage()} vs "
        f"artifact={artifact_level.to_storage()}:"
        f"{'通過' if allowed else '目的地分類下限不足,拒絕匯出'}"
    )
    pd = policy.record_decision(
        db,
        action="artifact.export",
        resource_type="artifact",
        resource_id=str(artifact.id),
        decision=decision,
        actor_type=caller.actor_type,
        actor_id=caller.actor_id,
        task_id=artifact.source_task_id,
        reason=reason,
        metadata={
            "target_space": payload.target_space,
            "target_classification_floor": target_floor.to_storage(),
            "artifact_classification_level": artifact_level.to_storage(),
        },
        commit=False,
    )
    if not allowed:
        if artifact.source_task_id is not None:
            task = db.get(Task, artifact.source_task_id)
            if task is not None and task.status not in (
                "completed", "failed", "cancelled", "blocked_by_policy"
            ):
                task.status = "blocked_by_policy"
                task.policy_decision_id = pd.id
                task.updated_at = datetime.now(timezone.utc)
        db.add(AuditLog(
            actor_user_id=caller.exporter_user_id,
            actor_username=caller.exporter_employee_id,
            action="artifact.export.denied",
            resource_type="artifact",
            resource_id=str(artifact.id),
            status="failure",
            detail=reason,
        ))
        db.flush()
        db.commit()
        raise HTTPException(
            status_code=403,
            detail="匯出遭 classification policy 拒絕:目的地分類下限不足",
        )
    exporter_user_id = caller.exporter_user_id
    exporter_employee_id = caller.exporter_employee_id
    if caller.actor_type == "service" and payload.employee_id:
        exporter_user_id, exporter_employee_id = artifacts.resolve_owner(
            db, requester_user_id=None, employee_id=payload.employee_id,
        )
    export = artifacts.record_export(
        db, artifact=artifact, payload=payload,
        exporter_user_id=exporter_user_id,
        exporter_employee_id=exporter_employee_id,
        policy_decision_id=pd.id, level=artifact_level,
        commit=False,
    )
    if artifact.source_task_id is not None:
        task = db.get(Task, artifact.source_task_id)
        if task is not None:
            task.policy_decision_id = pd.id
            task.updated_at = datetime.now(timezone.utc)
    db.add(AuditLog(
        actor_user_id=exporter_user_id,
        actor_username=exporter_employee_id,
        action="artifact.export.allowed",
        resource_type="artifact",
        resource_id=str(artifact.id),
        status="success",
        detail=reason,
    ))
    db.flush()
    db.commit()
    return ExportResult(
        export_id=export.id, classification_level=artifact_level,
        decision="allow",
    )


# ── /api 治理讀面 ─────────────────────────────────────────────────────────────


@router.get("/api/artifacts", response_model=list[ArtifactOut])
def list_artifacts_api(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    artifact_type: str | None = Query(None),
    task_id: int | None = Query(None),
    classification_level: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """列出目前使用者 owner 或由其 task 產生的 artifacts。"""
    return artifacts.list_artifacts(
        db, viewer_user_id=current_user.id,
        artifact_type=artifact_type, task_id=task_id,
        classification_level=classification_level,
        limit=limit, offset=offset,
    )


@router.get("/api/artifacts/{artifact_id}", response_model=ArtifactDetailOut)
def get_artifact_api(
    artifact_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """讀單一 artifact + versions + exports;非 owner → 403。"""
    artifact = _load_artifact_or_404(db, artifact_id)
    if artifact.status == "erased":
        raise HTTPException(status_code=410, detail="artifact 已完成清除")
    try:
        artifacts.ensure_artifact_access(
            db, artifact=artifact, viewer_user_id=current_user.id,
        )
    except PermissionError:
        raise HTTPException(
            status_code=403, detail="無權存取此 artifact"
        ) from None
    return artifact


def _require_job_selected_service(
    db: Session,
    *,
    task_id: int | None,
    caller: _ServiceCaller,
    lock: bool = False,
) -> Task | None:
    """Bind a Task-scoped ArtifactJob operation to its selected service.

    Legacy/dev jobs without a Task remain readable/mutable for backward
    compatibility, but once a job has a Task binding every service operation
    must present the credential of that Task's selected artifact service.
    """
    if task_id is None:
        return None
    query = db.query(Task).filter(Task.id == task_id)
    if lock:
        query = query.with_for_update()
    task = query.one_or_none()
    if task is None or (task.selected_service_id or "").strip() not in {
        str(caller.service.id),
        caller.service.slug,
    }:
        raise HTTPException(
            status_code=403,
            detail="ArtifactJob 與 Task 指派服務不符",
        )
    return task


@router.get("/api/artifacts/{artifact_id}/versions/{version_id}/download")
def download_artifact_version(
    artifact_id: int,
    version_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Authorized browser download from the CSP-owned immutable blob."""
    artifact = _load_artifact_or_404(db, artifact_id)
    version = db.query(ArtifactVersion).filter(
        ArtifactVersion.id == version_id
    ).with_for_update().one_or_none()
    if version is None or version.artifact_id != artifact.id:
        raise HTTPException(status_code=404, detail="找不到此 artifact version")
    if version.lifecycle_state in {"revoked", "erase_due", "erased"}:
        raise HTTPException(status_code=410, detail="artifact version 已撤銷")
    if (
        version.lifecycle_state == "archived"
        and not settings.RETENTION_ALLOW_ARCHIVED_DOWNLOADS
    ):
        raise HTTPException(status_code=410, detail="artifact version 已封存")
    if version.lifecycle_state not in {"active", "archived"}:
        raise HTTPException(status_code=409, detail="artifact lifecycle state 無效")
    if (
        not version.blob_key
        or not version.content_hash
        or version.blob_size_bytes is None
        or not version.media_type
        or not version.original_filename
    ):
        raise HTTPException(status_code=410, detail="legacy artifact 無 CSP authoritative blob")
    _artifact_download_authorized(
        db, artifact=artifact, version=version, user=current_user
    )
    try:
        path = resolve_blob_path(settings.ARTIFACT_BLOB_STORAGE_PATH, version.blob_key)
    except BlobValidationError as exc:
        raise HTTPException(status_code=409, detail="artifact blob metadata 無效") from exc
    if not path.is_file():
        raise HTTPException(status_code=410, detail="artifact blob 不存在")
    if path.stat().st_size != version.blob_size_bytes:
        raise HTTPException(status_code=409, detail="artifact blob size integrity failure")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != version.content_hash:
        raise HTTPException(status_code=409, detail="artifact blob hash integrity failure")

    db.add(
        AuditLog(
            actor_user_id=current_user.id,
            actor_username=current_user.username,
            action="artifact.downloaded",
            resource_type="artifact_version",
            resource_id=str(version.id),
            status="success",
            detail=f"artifact={artifact.id}; version={version.version}",
        )
    )
    db.commit()
    return FileResponse(
        path,
        media_type=version.media_type,
        filename=version.original_filename,
        headers={
            "Cache-Control": "private, no-store",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.post("/api/artifacts/{artifact_id}/versions/{version_id}/revoke")
def revoke_artifact_version(
    artifact_id: int,
    version_id: int,
    payload: ArtifactVersionRevokeIn,
    db: Session = Depends(get_db),
    manager: User = Depends(require_admin),
):
    """Revoke a version without deleting bytes (retention is Gate 3 A5)."""
    artifact = _load_artifact_or_404(db, artifact_id)
    version = (
        db.query(ArtifactVersion)
        .filter(
            ArtifactVersion.id == version_id,
            ArtifactVersion.artifact_id == artifact.id,
        )
        .with_for_update()
        .one_or_none()
    )
    if version is None:
        raise HTTPException(status_code=404, detail="找不到此 artifact version")
    if not version.is_active or version.revoked_at is not None:
        raise HTTPException(status_code=409, detail="artifact version 已撤銷")
    version.is_active = False
    version.lifecycle_state = "revoked"
    version.revoked_at = datetime.now(timezone.utc)
    version.erase_due_at = version.revoked_at + timedelta(
        days=settings.RETENTION_ARTIFACT_ARCHIVE_DAYS
    )
    version.revoked_by_user_id = manager.id
    version.revocation_reason = payload.reason.strip()
    artifact.active_version_count = max(0, (artifact.active_version_count or 0) - 1)
    db.add(
        AuditLog(
            actor_user_id=manager.id,
            actor_username=manager.username,
            action="artifact.version.revoked",
            resource_type="artifact_version",
            resource_id=str(version.id),
            status="success",
            detail=version.revocation_reason,
        )
    )
    db.commit()
    return {"artifact_id": artifact.id, "version_id": version.id, "revoked": True}


@router.post("/api/artifacts/{artifact_id}/versions/{version_id}/archive")
def archive_artifact_version(
    artifact_id: int,
    version_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    artifact = _load_artifact_or_404(db, artifact_id)
    version = db.query(ArtifactVersion).filter(
        ArtifactVersion.id == version_id
    ).with_for_update().one_or_none()
    if version is None or version.artifact_id != artifact.id:
        raise HTTPException(status_code=404, detail="找不到此 artifact version")
    _artifact_download_authorized(db, artifact=artifact, version=version, user=current_user)
    if version.lifecycle_state == "archived":
        return {"artifact_id": artifact.id, "version_id": version.id, "archived": True}
    if version.lifecycle_state != "active":
        raise HTTPException(status_code=409, detail="只有 active version 可封存")
    now = datetime.now(timezone.utc)
    version.lifecycle_state = "archived"
    version.is_active = False
    version.archived_at = now
    version.erase_due_at = now + timedelta(
        days=settings.RETENTION_ARTIFACT_ARCHIVE_DAYS
    )
    artifact.active_version_count = max(0, (artifact.active_version_count or 0) - 1)
    db.add(AuditLog(
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="artifact.version.archived",
        resource_type="artifact_version",
        resource_id=str(version.id),
        status="success",
    ))
    db.commit()
    return {"artifact_id": artifact.id, "version_id": version.id, "archived": True}


@router.post("/api/artifacts/{artifact_id}/versions/{version_id}/legal-hold")
def set_artifact_version_legal_hold(
    artifact_id: int,
    version_id: int,
    payload: ArtifactLegalHoldIn,
    db: Session = Depends(get_db),
    manager: User = Depends(require_admin),
):
    artifact = _load_artifact_or_404(db, artifact_id)
    version = db.query(ArtifactVersion).filter(
        ArtifactVersion.id == version_id
    ).with_for_update().one_or_none()
    if version is None or version.artifact_id != artifact.id:
        raise HTTPException(status_code=404, detail="找不到此 artifact version")
    if version.lifecycle_state == "erased":
        raise HTTPException(status_code=410, detail="artifact version 已清除")
    version.legal_hold = payload.held
    version.legal_hold_reason = payload.reason.strip() if payload.reason else None
    db.add(AuditLog(
        actor_user_id=manager.id,
        actor_username=manager.username,
        action="artifact.version.legal_hold.set",
        resource_type="artifact_version",
        resource_id=str(version.id),
        status="success",
        detail=version.legal_hold_reason or "released",
    ))
    db.commit()
    return {"artifact_id": artifact.id, "version_id": version.id, "held": version.legal_hold}


@router.get("/api/artifacts/{artifact_id}/download")
def download_current_artifact_version(
    artifact_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Stable browser URL resolving the current version inside CSP."""
    artifact = _load_artifact_or_404(db, artifact_id)
    version = (
        db.query(ArtifactVersion)
        .filter(
            ArtifactVersion.artifact_id == artifact.id,
            ArtifactVersion.version == artifact.current_version,
        )
        .one_or_none()
    )
    if version is None:
        raise HTTPException(status_code=410, detail="artifact current version 不存在")
    return download_artifact_version(
        artifact_id=artifact.id,
        version_id=version.id,
        db=db,
        current_user=current_user,
    )
