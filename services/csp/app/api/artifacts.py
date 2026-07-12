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

import hmac
import logging
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.middleware.caller import ACCESS_COOKIE_NAME, _extract_bearer, get_caller
from app.models.artifact import Artifact, ArtifactJob
from app.models.source_snapshot import SourceSnapshot
from app.models.task import Task
from app.models.user import User
from app.modules import artifacts, policy
from app.schemas.contracts.artifacts import (
    ArtifactDetailOut,
    ArtifactExportIn,
    ArtifactIn,
    ArtifactJobIn,
    ArtifactJobOut,
    ArtifactJobPatch,
    ArtifactOut,
    ArtifactRegisterResult,
    ArtifactVersionIn,
    ArtifactVersionResult,
    ExportResult,
)
from anila_contracts import Classification as ClassificationLevel
from app.services import agent_credential_service
from app.services.auth_service import get_current_user, is_admin_tier

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Artifact"])

_INHERIT_REASON = "source_selected"


# ── 認證依賴 ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _ServiceCaller:
    """/v1 service 面呼叫者(service token;legacy env 時 identity=None)。"""

    identity: object | None


def _resolve_service_token(request: Request, db: Session):
    """回 (matched: bool, identity)。matched=False 代表無有效 service token。"""
    token = request.headers.get("X-CSP-Service-Token")
    if not token:
        return False, None
    identity = agent_credential_service.verify_service_token(db, token=token)
    if identity is not None:
        request.state.csp_caller = identity
        return True, identity
    legacy = (settings.CSP_SERVICE_TOKEN or "").strip()
    if legacy and hmac.compare_digest(token, legacy):
        request.state.csp_caller = None  # legacy = unattributed
        return True, None
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
    if matched:
        return _ServiceCaller(identity=identity)
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


def require_export_caller(
    request: Request, db: Session = Depends(get_db)
) -> _ExportCaller:
    """匯出 gate 接受 user JWT 或 service token(doc 09 Artifact export)。"""
    matched, identity = _resolve_service_token(request, db)
    if matched:
        actor_id = str(getattr(identity, "id", 0) or 0)
        return _ExportCaller(
            actor_type="service", actor_id=actor_id,
            exporter_user_id=None, exporter_employee_id=None,
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
        task = db.query(Task).filter(Task.id == task_id).first()
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
    return task, snapshot


def _latch_inheritance(
    db: Session, *, artifact: Artifact, task: Task | None,
    snapshot: SourceSnapshot | None, actor_id: str,
    explicit_floor: ClassificationLevel | None = None,
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
    )
    db.refresh(artifact)
    return ClassificationLevel.from_storage(artifact.classification_level)


# ── /v1 service 面 ────────────────────────────────────────────────────────────


@router.post("/v1/artifact-jobs", response_model=ArtifactJobOut, status_code=201)
def register_artifact_job(
    payload: ArtifactJobIn,
    caller: _ServiceCaller = Depends(require_service_caller),
    db: Session = Depends(get_db),
):
    """冪等 upsert 一筆 Studio job(doc 02 ArtifactJob;restart 不丟失)。"""
    owner_user_id, employee_id = artifacts.resolve_owner(
        db, requester_user_id=payload.requester_user_id,
        employee_id=payload.employee_id,
    )
    job = artifacts.register_job(
        db, payload=payload, owner_user_id=owner_user_id,
        employee_id=employee_id,
    )
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
        return artifacts.transition_job(db, job_id=job_id, patch=patch)
    except LookupError:
        raise HTTPException(status_code=404, detail="找不到此 job") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post(
    "/v1/artifacts", response_model=ArtifactRegisterResult, status_code=201
)
def register_artifact(
    payload: ArtifactIn,
    caller: _ServiceCaller = Depends(require_service_caller),
    db: Session = Depends(get_db),
):
    """註冊成品 + 首版;binding 驗證 + 分類繼承(effective = max)。"""
    task, snapshot = _resolve_binding(
        db, task_id=payload.task_id,
        source_snapshot_id=payload.source_snapshot_id,
    )
    # owner / trace 由 task 優先、否則 job。
    owner_user_id: int | None = None
    trace_id: str | None = None
    if task is not None:
        owner_user_id = task.requester_user_id
        trace_id = task.trace_id
    elif payload.job_id:
        job = db.get(ArtifactJob, payload.job_id)
        if job is not None:
            owner_user_id = job.owner_user_id
            trace_id = job.trace_id

    explicit = payload.classification_level or ClassificationLevel.UNCLASSIFIED
    try:
        artifact = artifacts.create_artifact(
            db, payload=payload, owner_user_id=owner_user_id,
            source_task_id=payload.task_id,
            source_snapshot_id=payload.source_snapshot_id,
            trace_id=trace_id, initial_level=explicit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    effective = _latch_inheritance(
        db, artifact=artifact, task=task, snapshot=snapshot,
        actor_id=str(owner_user_id or 0),
    )
    version = artifacts.create_version(
        db, artifact=artifact, storage_ref=payload.storage_ref,
        content_hash=payload.content_hash, file_refs=payload.file_refs,
        citation_map=payload.citation_map,
        generated_by_model_id=payload.generated_by_model_id,
        generated_by_agent_id=payload.generated_by_agent_id,
        generated_by_studio_job_id=payload.job_id, level=effective,
    )
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
    artifact = _load_artifact_or_404(db, artifact_id)
    task, snapshot = _resolve_binding(
        db, task_id=artifact.source_task_id,
        source_snapshot_id=artifact.source_snapshot_id,
    )
    effective = _latch_inheritance(
        db, artifact=artifact, task=task, snapshot=snapshot,
        actor_id=str(artifact.owner_user_id or 0),
        explicit_floor=payload.classification_level,
    )
    version = artifacts.create_version(
        db, artifact=artifact, storage_ref=payload.storage_ref,
        content_hash=payload.content_hash, file_refs=payload.file_refs,
        citation_map=payload.citation_map,
        generated_by_model_id=payload.generated_by_model_id,
        generated_by_agent_id=payload.generated_by_agent_id,
        generated_by_studio_job_id=payload.generated_by_studio_job_id,
        level=effective,
    )
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
    )
    if not allowed:
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
    )
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
    """列出 artifacts(admin/owner: 全部;一般使用者: 自己 owner 或 task 申請人)。"""
    return artifacts.list_artifacts(
        db, viewer_user_id=current_user.id,
        is_admin=is_admin_tier(current_user),
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
    """讀單一 artifact + versions + exports;非 owner/admin → 403。"""
    artifact = _load_artifact_or_404(db, artifact_id)
    try:
        artifacts.ensure_artifact_access(
            db, artifact=artifact, viewer_user_id=current_user.id,
            is_admin=is_admin_tier(current_user),
        )
    except PermissionError:
        raise HTTPException(
            status_code=403, detail="無權存取此 artifact"
        ) from None
    return artifact
