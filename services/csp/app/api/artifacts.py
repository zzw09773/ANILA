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
from app.schemas.contracts.classification import ClassificationLevel
from app.services import agent_credential_service
from app.services.auth_service import get_current_user, is_admin_tier

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Artifact"])

_INHERIT_REASON = "source_selected"


# ── 認證依賴 ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _ServiceCaller:
    """/v1 service 面呼叫者(dispatch JWT / service_client / legacy)。"""

    identity: object | None
    # Set only for dispatch-JWT agent callers (claims.user_id). Used to
    # pin job owner attribution; None for service_client / legacy.
    dispatch_user_id: int | None = None


def _looks_like_dispatch_jwt(token: str) -> bool:
    """True when unverified claims declare the dispatch audience."""
    from jose import JWTError, jwt

    from app.services.proxy.dispatch_token import DISPATCH_TOKEN_AUDIENCE

    try:
        claims = jwt.get_unverified_claims(token)
    except JWTError:
        return False
    aud = claims.get("aud")
    if isinstance(aud, list):
        return DISPATCH_TOKEN_AUDIENCE in aud
    return aud == DISPATCH_TOKEN_AUDIENCE


# 成品寫入與匯出不接受派工 JWT。聊天與知識庫搜尋才是它的用途。
DISPATCH_JWT_REJECTED_DETAIL = "派工 JWT 僅能用於聊天與知識庫搜尋"


def _reject_dispatch_jwt(request: Request) -> None:
    """看到派工 JWT 就拒絕。成品面只收服務憑證，不把提問者身分借給 agent。

    沒有派工形狀的 token 時直接返回，讓 service-client / legacy 繼續。
    Bearer 或 X-CSP-Service-Token 任一是派工 JWT 都是 401，不改試另一張憑證。
    """
    from app.services.proxy.dispatch_token import (
        extract_bearer_token,
        verify_dispatch_token,
    )

    candidates: list[str] = []
    bearer = extract_bearer_token(request.headers.get("Authorization"))
    if bearer:
        candidates.append(bearer)
    header_token = request.headers.get("X-CSP-Service-Token")
    if header_token and header_token not in candidates:
        candidates.append(header_token)

    for token in candidates:
        if token.startswith("csk-"):
            continue
        if verify_dispatch_token(token) is not None or _looks_like_dispatch_jwt(token):
            raise HTTPException(
                status_code=401, detail=DISPATCH_JWT_REJECTED_DETAIL
            )
    return None


def _resolve_service_token(request: Request, db: Session):
    """回 (matched, identity, dispatch_user_id)。matched=False 代表無有效憑證。

    憑證順序:
      1. 派工 JWT（Bearer 或 X-CSP-Service-Token）→ 401，不落到其他憑證
      2. service_client ``csk-`` / legacy fleet token via X-CSP-Service-Token
      3. bare agent ``csk-`` → 401 (retired; no dual-accept)
    """
    _reject_dispatch_jwt(request)

    token = request.headers.get("X-CSP-Service-Token")
    if not token:
        return False, None, None

    if agent_credential_service.fleet_secret_retired(token):
        raise HTTPException(status_code=401, detail="服務權杖無效")

    identity = agent_credential_service.verify_service_token(db, token=token)
    if identity is not None:
        if identity.kind == "agent":
            raise HTTPException(
                status_code=401,
                detail="agent 任務回呼請使用派工 JWT，不再接受 csk-",
            )
        request.state.csp_caller = identity
        return True, identity, None

    legacy = (settings.CSP_SERVICE_TOKEN or "").strip()
    if legacy and hmac.compare_digest(token, legacy):
        request.state.csp_caller = None  # legacy = unattributed
        return True, None, None
    # header present but invalid → 明確 401(不是 403)。
    raise HTTPException(status_code=401, detail="服務權杖無效")


def require_service_caller(
    request: Request, db: Session = Depends(get_db)
) -> _ServiceCaller:
    """/v1 寫入面 gate: service_client 或 legacy service token。

    派工 JWT → 401。無服務憑證但帶使用者憑證(JWT/cookie)→ 403
    (此端點不開放使用者直建 artifact/job);完全匿名 → 401。
    """
    matched, identity, dispatch_user_id = _resolve_service_token(request, db)
    if matched:
        return _ServiceCaller(
            identity=identity, dispatch_user_id=dispatch_user_id
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
        status_code=401,
        detail="缺少 X-CSP-Service-Token(服務對服務端點)",
    )


def _is_agent_caller(caller: _ServiceCaller) -> bool:
    identity = caller.identity
    return identity is not None and getattr(identity, "kind", None) == "agent"


def _require_dispatch_user_id(caller: _ServiceCaller) -> int:
    if caller.dispatch_user_id is None:
        raise HTTPException(
            status_code=401, detail="dispatch JWT 缺少 user_id"
        )
    return caller.dispatch_user_id


def _enforce_agent_requester_scope(
    caller: _ServiceCaller, *, requester_user_id: int | None, employee_id: str | None
) -> int:
    """Agent-kind callers may only attribute jobs to the dispatch JWT user_id."""
    if not _is_agent_caller(caller):
        raise RuntimeError("_enforce_agent_requester_scope called for non-agent")
    dispatch_uid = _require_dispatch_user_id(caller)
    if requester_user_id is not None and requester_user_id != dispatch_uid:
        raise HTTPException(
            status_code=403,
            detail="agent 不得指定非派工對象的 requester_user_id",
        )
    if (employee_id or "").strip():
        raise HTTPException(
            status_code=403,
            detail="agent 不得以 employee_id 指定 owner",
        )
    return dispatch_uid


def _enforce_agent_owner_match(
    caller: _ServiceCaller, *, owner_user_id: int | None, resource: str
) -> None:
    """Reject agent-kind callers that reference another user's resource.

    The dispatch JWT already carries ``user_id`` (plumbed as
    ``caller.dispatch_user_id``). Matching that against the resource owner's
    user id closes cross-user laundering via foreign ``task_id`` /
    ``job_id`` / ``artifact_id`` — no ``task_id`` claim is required.
    """
    if not _is_agent_caller(caller):
        return
    dispatch_uid = _require_dispatch_user_id(caller)
    if owner_user_id is None or owner_user_id != dispatch_uid:
        raise HTTPException(
            status_code=403,
            detail=f"agent 不得參照其他使用者的 {resource}",
        )


def _missing_resource(
    caller: _ServiceCaller | None, *, resource: str, detail: str
) -> HTTPException:
    """404 for service/user; agents get the same 403 as a foreign hit.

    Closes the exists-but-foreign (403) vs missing (404) oracle on the
    write faces without weakening the owner-match deny.
    """
    if caller is not None and _is_agent_caller(caller):
        return HTTPException(
            status_code=403,
            detail=f"agent 不得參照其他使用者的 {resource}",
        )
    return HTTPException(status_code=404, detail=detail)


@dataclass(frozen=True)
class _ExportCaller:
    """匯出面呼叫者:user JWT 或 service_client／legacy service token。"""

    actor_type: str
    actor_id: str
    exporter_user_id: int | None
    exporter_employee_id: str | None
    is_admin: bool = False
    # True when actor is a registered service_client (not agent csk-).
    may_attribute_employee: bool = False


def require_export_caller(
    request: Request, db: Session = Depends(get_db)
) -> _ExportCaller:
    """匯出 gate 接受 user JWT 或 service_client／legacy service token。

    Agent ``csk-`` credentials are accepted by the shared verify helper for
    other s2s paths, but must not open artifact export — that would let any
    developer-issued agent token bypass ``ensure_artifact_access``.
    """
    matched, identity, _dispatch_user_id = _resolve_service_token(request, db)
    if matched:
        if identity is not None and getattr(identity, "kind", None) == "agent":
            raise HTTPException(
                status_code=403,
                detail="artifact 匯出僅接受 service_client 或使用者憑證",
            )
        if identity is None:
            actor_id = "legacy"
            may_attr = True  # legacy fleet token (Studio / tests)
        else:
            actor_id = str(
                getattr(identity, "service_client_id", None)
                or getattr(identity, "credential_id", 0)
                or 0
            )
            may_attr = getattr(identity, "kind", None) == "service_client"
        return _ExportCaller(
            actor_type="service", actor_id=actor_id,
            exporter_user_id=None, exporter_employee_id=None,
            is_admin=False, may_attribute_employee=may_attr,
        )
    # 落到使用者 JWT / sk- API key / cookie(匿名由 get_caller fail-closed 401)。
    user = get_caller(request, db).user
    return _ExportCaller(
        actor_type="user", actor_id=str(user.id),
        exporter_user_id=user.id, exporter_employee_id=user.username,
        is_admin=is_admin_tier(user),
        may_attribute_employee=False,
    )


# ── 內部 helpers ─────────────────────────────────────────────────────────────


def _load_artifact_or_404(
    db: Session,
    artifact_id: int,
    caller: _ServiceCaller | None = None,
) -> Artifact:
    artifact = artifacts.get_artifact(db, artifact_id)
    if artifact is None:
        raise _missing_resource(
            caller, resource="artifact", detail="找不到此 artifact",
        )
    return artifact


def _resolve_binding(
    db: Session,
    *,
    task_id: int | None,
    source_snapshot_id: int | None,
    caller: _ServiceCaller | None = None,
) -> tuple[Task | None, SourceSnapshot | None]:
    """存在性驗證:給了 id 就必須查得到列,否則 404(agent → 403)。"""
    task = None
    snapshot = None
    if task_id is not None:
        task = db.query(Task).filter(Task.id == task_id).first()
        if task is None:
            raise _missing_resource(
                caller, resource="task", detail=f"找不到 task {task_id}",
            )
    if source_snapshot_id is not None:
        snapshot = db.query(SourceSnapshot).filter(
            SourceSnapshot.id == source_snapshot_id
        ).first()
        if snapshot is None:
            raise _missing_resource(
                caller,
                resource="source_snapshot",
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
    identity = caller.identity
    if identity is not None and getattr(identity, "kind", None) == "agent":
        pinned = _enforce_agent_requester_scope(
            caller,
            requester_user_id=payload.requester_user_id,
            employee_id=payload.employee_id,
        )
        owner_user_id, employee_id = artifacts.resolve_owner(
            db, requester_user_id=pinned, employee_id=None,
        )
    else:
        owner_user_id, employee_id = artifacts.resolve_owner(
            db, requester_user_id=payload.requester_user_id,
            employee_id=payload.employee_id,
        )
    # Upsert must not take over an existing row owned by someone else.
    # register_job keys on job_id and would otherwise overwrite owner /
    # status / trace while echoing unmanaged fields (result_metadata, …).
    existing = db.get(ArtifactJob, payload.job_id)
    if existing is not None and _is_agent_caller(caller):
        _enforce_agent_owner_match(
            caller, owner_user_id=existing.owner_user_id, resource="job",
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
    """狀態轉移 + 進度/回填;非法轉移 → 409、查無 → 404(agent → 403)。"""
    job = db.get(ArtifactJob, job_id)
    if job is None:
        raise _missing_resource(
            caller, resource="job", detail="找不到此 job",
        )
    # F3: agent may not patch another user's job (owner_user_id pin).
    _enforce_agent_owner_match(
        caller, owner_user_id=job.owner_user_id, resource="job",
    )
    try:
        return artifacts.transition_job(db, job_id=job_id, patch=patch)
    except LookupError:
        raise _missing_resource(
            caller, resource="job", detail="找不到此 job",
        ) from None
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
    """註冊成品 + 首版;binding 驗證 + 分類繼承(effective = max)。

    All four /v1 write faces scope agent-kind callers by
    ``caller.dispatch_user_id``: register job (including upsert over an
    existing row), patch job, register artifact (task / snapshot / job
    owners), and add version. Foreign ids cannot launder ownership,
    ``trace_id``, or classification into the written row.
    """
    task, snapshot = _resolve_binding(
        db, task_id=payload.task_id,
        source_snapshot_id=payload.source_snapshot_id,
        caller=caller,
    )
    job: ArtifactJob | None = None
    if payload.job_id:
        job = db.get(ArtifactJob, payload.job_id)

    # F3: reject foreign task / snapshot / job BEFORE copying any field
    # into owner_user_id / trace_id / ClassificationEvent actor_id.
    if task is not None:
        _enforce_agent_owner_match(
            caller, owner_user_id=task.requester_user_id, resource="task",
        )
    if snapshot is not None:
        snap_task = db.query(Task).filter(Task.id == snapshot.task_id).first()
        _enforce_agent_owner_match(
            caller,
            owner_user_id=(
                snap_task.requester_user_id if snap_task is not None else None
            ),
            resource="source_snapshot",
        )
    if job is not None:
        _enforce_agent_owner_match(
            caller, owner_user_id=job.owner_user_id, resource="job",
        )

    # owner / trace 由 task 優先、否則 job。
    owner_user_id: int | None = None
    trace_id: str | None = None
    if task is not None:
        owner_user_id = task.requester_user_id
        trace_id = task.trace_id
    elif job is not None:
        owner_user_id = job.owner_user_id
        trace_id = job.trace_id

    # actor_type is always "service" here; ClassificationEvent.actor_user_id
    # is only populated when actor_type == USER, so this value is not
    # observable on the event row.
    actor_id = str(owner_user_id or 0)

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
        actor_id=actor_id,
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
    artifact = _load_artifact_or_404(db, artifact_id, caller=caller)
    # F3: agent may not version another user's artifact.
    _enforce_agent_owner_match(
        caller, owner_user_id=artifact.owner_user_id, resource="artifact",
    )
    task, snapshot = _resolve_binding(
        db, task_id=artifact.source_task_id,
        source_snapshot_id=artifact.source_snapshot_id,
        caller=caller,
    )
    # actor_type is always "service" here; ClassificationEvent.actor_user_id
    # is only populated when actor_type == USER, so this value is not
    # observable on the event row.
    actor_id = str(artifact.owner_user_id or 0)
    effective = _latch_inheritance(
        db, artifact=artifact, task=task, snapshot=snapshot,
        actor_id=actor_id,
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
    """匯出 policy gate(SYSTEM-MAP §8 L241-242 兩條線)。

    allow iff artifact.level ≤ 營業秘密;deny → 403。
    PolicyDecision 在 allow/deny 皆落列;≥ 營業秘密 的 allow 即為稽核列
    (L242)。target_classification_floor 僅記 metadata,不作判定軸。
    """
    from app.schemas.contracts.classification import (
        classification_audit_required,
        outbound_action_allowed,
    )

    artifact = _load_artifact_or_404(db, artifact_id)
    # User JWT 面與 GET /api/artifacts/{id} 共用同一物件授權謂詞
    # (ensure_artifact_access);service_client／legacy 面維持 Studio 匯出。
    if caller.actor_type == "user" and caller.exporter_user_id is not None:
        try:
            artifacts.ensure_artifact_access(
                db,
                artifact=artifact,
                viewer_user_id=caller.exporter_user_id,
                is_admin=caller.is_admin,
            )
        except PermissionError:
            # Record the refused attempt before surfacing 403 (same posture
            # as message_action access_denied audit).
            policy.record_decision(
                db,
                action="artifact.export",
                resource_type="artifact",
                resource_id=str(artifact.id),
                decision="deny",
                actor_type=caller.actor_type,
                actor_id=caller.actor_id,
                task_id=artifact.source_task_id,
                reason="無權存取此 artifact",
                metadata={"deny_reason": "access_denied"},
            )
            raise HTTPException(
                status_code=403, detail="無權存取此 artifact"
            ) from None
    artifact_level = ClassificationLevel.from_storage(artifact.classification_level)
    target_floor = payload.target_classification_floor
    # SYSTEM-MAP §8 L241:可以做 = 密等 ≤ 營業秘密。
    allowed = outbound_action_allowed(artifact_level)
    decision = "allow" if allowed else "deny"
    reason = (
        f"匯出判定 artifact={artifact_level.to_storage()}:"
        f"{'通過(≤營業秘密)' if allowed else '密等超過營業秘密,拒絕匯出'}"
        f" (SYSTEM-MAP §8 L241)"
    )
    # SYSTEM-MAP §8 L242:≥ 營業秘密 必落稽核;deny 亦一律記。
    # 無機密 allow 仍記 PolicyDecision(既有契約),但規格「落稽核」閾在營業秘密。
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
            "target_classification_floor": (
                target_floor.to_storage() if target_floor is not None else None
            ),
            "artifact_classification_level": artifact_level.to_storage(),
            "audit_required": classification_audit_required(artifact_level),
        },
    )
    if not allowed:
        raise HTTPException(
            status_code=403,
            detail="匯出遭 classification policy 拒絕:密等超過營業秘密",
        )
    exporter_user_id = caller.exporter_user_id
    exporter_employee_id = caller.exporter_employee_id
    if (
        caller.actor_type == "service"
        and caller.may_attribute_employee
        and payload.employee_id
    ):
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
    collection_id: int | None = Query(None, ge=1),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """列出 artifacts(admin/owner: 全部;一般使用者: 自己 owner 或 task 申請人)。

    ``collection_id`` 把清單收成一個知識庫。知識庫不在 artifacts 表上，
    由 job、metadata、task、snapshot 彙出來。
    """
    return artifacts.list_artifacts(
        db, viewer_user_id=current_user.id,
        is_admin=is_admin_tier(current_user),
        artifact_type=artifact_type, task_id=task_id,
        classification_level=classification_level,
        collection_id=collection_id,
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
    artifacts.attach_collection_scope(db, [artifact])
    return artifact
