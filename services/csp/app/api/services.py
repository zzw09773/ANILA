# -*- coding: utf-8 -*-
"""Service Registry API (doc 07) — CRUD, Launch Gateway, audit callbacks.

Layering: this ``app.api`` module orchestrates the launch flow by composing the
pure ``app.modules.launch`` primitives with ``app.modules.policy`` (PolicyDecision)
and ``app.services.access_control`` (the 8-step algorithm). The launch module
itself imports none of these (module-boundary contract), so the independence /
api-layering import-linter contracts stay green.

Routes
------
  GET    /api/services                                accessible list
  POST   /api/services                                create (admin/owner)
  GET    /api/services/{service_id}                   detail (access-gated)
  PUT    /api/services/{service_id}                   update (admin/owner full;
                                                       per-service admin → only
                                                       db_editable_fields)
  DELETE /api/services/{service_id}                   soft-delete (admin/owner)
  POST   /api/services/{service_id}/launch            Launch Gateway
  POST   /api/services/{service_id}/audit-callbacks   Bearer Service Client Token
  GET    /api/services/{service_id}/manifest          fetch well-known manifest
  GET    /api/services/{service_id}/project-bindings  list bindings
  POST   /api/services/{service_id}/project-bindings  bind (admin/owner)
  DELETE /api/services/{service_id}/project-bindings/{binding_id}  unbind
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.registered_service import (
    RegisteredService,
    ServiceProjectBinding,
)
from app.models.source_snapshot import SourceSnapshot
from app.models.task import Task
from app.models.user import User
from app.modules import launch as launch_mod
from app.modules import policy as policy_mod
from app.modules.launch import ManifestFetchError
from app.schemas.contracts.classification import ClassificationLevel as _CL
from app.schemas.registered_service import (
    AuditCallbackPayload,
    AuditCallbackResponse,
    LaunchRequest,
    LaunchResponse,
    ProjectBindingCreate,
    ProjectBindingResponse,
    RegisteredServiceCreate,
    RegisteredServiceResponse,
    RegisteredServiceUpdate,
)
from app.services import agent_credential_service
from app.services.access_control import can_access_service
from app.services.audit_service import log_audit_event
from app.services.auth_service import get_current_user, is_admin_tier, require_admin
from app.utils.slug import unique_slug

router = APIRouter(prefix="/api/services", tags=["Service Registry"])

_UNCLASSIFIED = _CL.UNCLASSIFIED.value
_AUDIT_MAX_BYTES = 16 * 1024

# R-SEC (ADR-0008): fields a per-service admin may NEVER edit, even when an
# admin whitelists them in ``db_editable_fields``. ``service_client_id`` binds
# the service's audit-write identity — a delegate self-binding would grant
# themselves the audit-write channel (self-serving delegation), so this stays
# admin-tier only regardless of the db_editable_fields whitelist.
_ADMIN_ONLY_FIELDS = frozenset({"service_client_id"})


# ── helpers ─────────────────────────────────────────────────────────────────


def _resolve_service(db: Session, service_id: str) -> RegisteredService | None:
    """Resolve a {service_id} path param by numeric id or by slug (doc §10
    uses the slug, e.g. "material-analysis")."""
    q = db.query(RegisteredService)
    if service_id.isdigit():
        row = q.filter(RegisteredService.id == int(service_id)).first()
        if row is not None:
            return row
    return q.filter(RegisteredService.slug == service_id).first()


def _service_or_404(db: Session, service_id: str) -> RegisteredService:
    service = _resolve_service(db, service_id)
    if service is None:
        raise HTTPException(status_code=404, detail="服務不存在")
    return service


def _is_service_admin(user: User, service: RegisteredService) -> bool:
    """Per-service delegation (doc §3 note): membership in the service's own
    ``service_admin_user_ids`` — NOT a global role."""
    return user.id in (service.service_admin_user_ids or [])


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer 整合金鑰")
    return authorization[7:].strip()


def _url_origin(raw_url: str) -> str:
    parsed = urlparse((raw_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="服務 entry_url 必須是 http(s) URL")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="服務 entry_url 不可包含帳密")
    host = parsed.hostname or ""
    try:
        parsed_port = parsed.port
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="服務 entry_url port 無效",
        ) from exc
    port = f":{parsed_port}" if parsed_port is not None else ""
    return f"{parsed.scheme}://{host.lower()}{port}"


def _validate_launch_entry_url(service: RegisteredService) -> None:
    """Fail closed before appending a launch token to service.entry_url."""
    origin = _url_origin(service.entry_url)
    allowed = {
        _url_origin(candidate)
        for candidate in (service.allowed_origins or [])
        if candidate
    }
    if allowed and origin not in allowed:
        raise HTTPException(
            status_code=400,
            detail="服務 entry_url origin 不在 allowed_origins",
        )


def _validate_source_snapshot_access(
    db: Session,
    user: User,
    source_snapshot_id: int | None,
    *,
    task_id: int | None,
) -> None:
    if source_snapshot_id is None:
        return
    snapshot = db.get(SourceSnapshot, source_snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="來源快照不存在")
    if task_id is not None and snapshot.task_id != task_id:
        raise HTTPException(status_code=403, detail="來源快照不屬於此任務")
    task = db.get(Task, snapshot.task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="來源快照任務不存在")
    if task.requester_user_id != user.id and not is_admin_tier(user):
        raise HTTPException(status_code=403, detail="無權使用此來源快照")


# ── CRUD ────────────────────────────────────────────────────────────────────


@router.get("", response_model=list[RegisteredServiceResponse])
def list_services(
    include_inactive: bool = False,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Accessible services for regular users (mirrors accessible_links_for +
    classification handled at launch time); admin/owner see all."""
    from app.services.access_control import accessible_services_for

    if is_admin_tier(current_user):
        q = db.query(RegisteredService).order_by(
            RegisteredService.sort_order, RegisteredService.created_at
        )
        if not include_inactive:
            q = q.filter(RegisteredService.is_active.is_(True))
        return q.all()
    return accessible_services_for(db, current_user)


@router.post("", response_model=RegisteredServiceResponse, status_code=201)
def create_service(
    request: RegisteredServiceCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    data = request.model_dump()
    taken = {row[0] for row in db.query(RegisteredService.slug).all()}
    slug = data.get("slug") or None
    if slug and slug in taken:
        raise HTTPException(status_code=409, detail=f"slug「{slug}」已存在")
    if not slug:
        slug = unique_slug(data["name"], taken, fallback="service")
    ceiling = data.get("classification_ceiling")
    service = RegisteredService(
        name=data["name"],
        slug=slug,
        description=data.get("description"),
        icon=data.get("icon"),
        owner_department_id=data.get("owner_department_id"),
        owner_admin_user_id=data.get("owner_admin_user_id") or admin.id,
        service_admin_user_ids=data.get("service_admin_user_ids") or [],
        service_type=(data.get("service_type") or "project_portal"),
        project_entry=data.get("project_entry", False),
        project_id=data.get("project_id"),
        entry_url=data["entry_url"],
        allowed_origins=data.get("allowed_origins") or [],
        launch_mode=(data.get("launch_mode") or "new_tab"),
        iframe_allowed=data.get("iframe_allowed", False),
        sso_mode=(data.get("sso_mode") or "card_sso"),
        supports_launch_token=data.get("supports_launch_token", False),
        data_ingress=data.get("data_ingress") or [],
        data_egress=data.get("data_egress") or [],
        healthcheck_url=data.get("healthcheck_url"),
        audit_callback_url=data.get("audit_callback_url"),
        trace_callback_url=data.get("trace_callback_url"),
        # R-SEC (ADR-0008): admin-tier only — create_service is require_admin.
        service_client_id=data.get("service_client_id"),
        classification_ceiling=(ceiling.value if hasattr(ceiling, "value") else ceiling),
        required_roles=data.get("required_roles") or [],
        is_public=data.get("is_public", False),
        # doc §11: default private (default-deny access grants).
        is_active=True,
        config_source="db",
        db_editable_fields=[],
        sort_order=data.get("sort_order", 0),
    )
    db.add(service)
    db.commit()
    db.refresh(service)
    log_audit_event(
        db,
        actor=admin,
        action="service.create",
        resource_type="registered_service",
        resource_id=service.id,
        detail=f"上架服務「{service.name}」(slug={service.slug})",
        metadata={"slug": service.slug, "config_source": "db"},
        commit=True,
    )
    return service


@router.get("/{service_id}", response_model=RegisteredServiceResponse)
def get_service(
    service_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = _service_or_404(db, service_id)
    if not can_access_service(db, current_user, service):
        raise HTTPException(status_code=403, detail="無權存取此服務")
    return service


@router.put("/{service_id}", response_model=RegisteredServiceResponse)
def update_service(
    service_id: str,
    request: RegisteredServiceUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = _service_or_404(db, service_id)
    admin_tier = is_admin_tier(current_user)
    if not (admin_tier or _is_service_admin(current_user, service)):
        raise HTTPException(status_code=403, detail="需要管理員或該服務管理員權限")

    update_data = request.model_dump(exclude_unset=True)
    if not admin_tier:
        # R-SEC (ADR-0008): admin-only fields are off-limits to a per-service
        # admin BEFORE the db_editable_fields whitelist is even consulted — so
        # a delegate cannot self-bind their audit-write identity even if an
        # admin mistakenly whitelisted service_client_id in db_editable_fields.
        admin_only = _ADMIN_ONLY_FIELDS.intersection(update_data)
        if admin_only:
            raise HTTPException(
                status_code=403,
                detail="該服務管理員不可變更 audit-callback 綁定"
                f"({sorted(admin_only)} 僅限系統管理員)",
            )
        # Per-service admin may only edit whitelisted db_editable_fields.
        allowed = set(service.db_editable_fields or [])
        blocked = [f for f in update_data if f not in allowed]
        if blocked:
            raise HTTPException(
                status_code=403,
                detail=f"該服務管理員僅可編輯 {sorted(allowed)};不可改 {blocked}",
            )
    for field, value in update_data.items():
        if field == "classification_ceiling" and hasattr(value, "value"):
            value = value.value
        elif hasattr(value, "value"):  # enum fields (service_type/launch_mode/…)
            value = value.value
        setattr(service, field, value)
    db.commit()
    db.refresh(service)
    log_audit_event(
        db,
        actor=current_user,
        action="service.update",
        resource_type="registered_service",
        resource_id=service.id,
        detail=f"更新服務「{service.name}」"
        + ("" if admin_tier else "(per-service admin)"),
        metadata={"fields": sorted(update_data.keys())},
        commit=True,
    )
    return service


@router.delete("/{service_id}")
def deactivate_service(
    service_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Soft-delete (deactivate). Grants/launches/audit history are preserved
    (doc §14 blocker); a hard purge lives on the compat /api/platform-links."""
    service = _service_or_404(db, service_id)
    service.is_active = False
    db.commit()
    log_audit_event(
        db,
        actor=admin,
        action="service.deactivate",
        resource_type="registered_service",
        resource_id=service.id,
        detail=f"停用服務「{service.name}」",
        commit=True,
    )
    return {"message": "服務已停用"}


# ── Launch Gateway ──────────────────────────────────────────────────────────


@router.post("/{service_id}/launch", response_model=LaunchResponse)
def launch_service(
    service_id: str,
    request: LaunchRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = _service_or_404(db, service_id)

    # Resolve classification / trace / snapshot context. A task ties the launch
    # to its classification + trace; otherwise the caller declares a floor.
    task_id: int | None = None
    trace_id: str | None = None
    source_snapshot_id: int | None = request.source_snapshot_id
    if request.classification_level is not None:
        level = request.classification_level.value
    else:
        level = _UNCLASSIFIED
    if request.task_id is not None:
        task = db.query(Task).filter(Task.id == request.task_id).first()
        if task is None:
            raise HTTPException(status_code=404, detail="任務不存在")
        if task.requester_user_id != current_user.id and not is_admin_tier(
            current_user
        ):
            raise HTTPException(status_code=403, detail="無權以此任務啟動服務")
        task_id = task.id
        level = task.classification_level
        trace_id = task.trace_id
        if source_snapshot_id is None:
            source_snapshot_id = task.source_snapshot_id
    if trace_id is None:
        trace_id = launch_mod.new_trace_id()
    _validate_source_snapshot_access(
        db, current_user, source_snapshot_id, task_id=task_id
    )
    _validate_launch_entry_url(service)

    # 8-step access algorithm with the launch classification as context_level:
    # step 6 (classification clearance) denies when level > service ceiling.
    if not can_access_service(db, current_user, service, context_level=level):
        policy_mod.record_decision(
            db,
            action="service.launch",
            resource_type="registered_service",
            resource_id=service.slug,
            decision="deny",
            actor_type="user",
            actor_id=str(current_user.id),
            task_id=task_id,
            reason="access denied: role/grant/classification gate failed",
            metadata={"classification_level": level, "service_id": service.slug},
        )
        log_audit_event(
            db,
            actor=current_user,
            action="service.launch",
            resource_type="registered_service",
            resource_id=service.id,
            status="denied",
            detail=f"拒絕啟動服務「{service.name}」(access gate)",
            commit=True,
        )
        raise HTTPException(status_code=403, detail="無權啟動此服務")

    launch_id = launch_mod.new_launch_id()
    row = launch_mod.create_service_launch(
        db,
        launch_id=launch_id,
        service_id=service.id,
        user_id=current_user.id,
        task_id=task_id,
        trace_id=trace_id,
        classification_level=level,
        source_snapshot_id=source_snapshot_id,
        mode=service.launch_mode,
    )
    claims = launch_mod.build_launch_claims(
        aud=service.slug,
        launch_id=launch_id,
        service_id=service.slug,
        user_id=current_user.id,
        employee_id=getattr(current_user, "employee_id", None)
        or current_user.username,
        department_id=current_user.department_id,
        roles=[current_user.role],
        task_id=str(task_id) if task_id is not None else None,
        trace_id=trace_id,
        classification_level=level,
        source_snapshot_id=str(source_snapshot_id)
        if source_snapshot_id is not None
        else None,
        issued_at=row.issued_at,
        expires_at=row.expires_at,
    )
    token = launch_mod.issue_launch_token(claims)
    launch_url = launch_mod.build_launch_url(service.entry_url, token)

    # record_decision commits the launch row in the same session/transaction.
    policy_mod.record_decision(
        db,
        action="service.launch",
        resource_type="registered_service",
        resource_id=service.slug,
        decision="allow",
        actor_type="user",
        actor_id=str(current_user.id),
        task_id=task_id,
        metadata={
            "launch_id": launch_id,
            "classification_level": level,
            "service_id": service.slug,
        },
    )
    log_audit_event(
        db,
        actor=current_user,
        action="service.launch",
        resource_type="registered_service",
        resource_id=service.id,
        detail=f"啟動服務「{service.name}」(launch_id={launch_id})",
        metadata={"launch_id": launch_id, "trace_id": trace_id, "mode": row.mode},
        commit=True,
    )
    return LaunchResponse(
        launch_id=launch_id,
        launch_token=token,
        launch_url=launch_url,
        mode=row.mode,
        expires_at=row.expires_at,
    )


# ── Audit callback (doc §10) ────────────────────────────────────────────────


@router.post(
    "/{service_id}/audit-callbacks",
    response_model=AuditCallbackResponse,
    status_code=201,
)
def audit_callback(
    service_id: str,
    payload: AuditCallbackPayload,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """Registered service posts an audit event back to CSP, authenticated with
    its Service Client Token (doc 03 naming). Bad key → 401; oversized → 413."""
    token = _bearer_token(authorization)
    identity = agent_credential_service.verify_service_token(db, token=token)
    if identity is None or identity.kind != "service_client":
        raise HTTPException(status_code=401, detail="整合金鑰無效")

    service = _service_or_404(db, service_id)

    # R-SEC (ADR-0008): fail-closed client↔service binding. Authenticating a
    # valid Service Client Token is NOT sufficient — the presented client must
    # be the one bound to THIS service, else any integration-key holder could
    # inject audit events for any service (cross-service audit-trail pollution).
    if service.service_client_id is None:
        log_audit_event(
            db,
            actor=None,
            action="service.audit_callback",
            resource_type="registered_service",
            resource_id=service.id,
            status="denied",
            detail="拒絕 audit callback:服務尚未綁定 Service Client Token",
            metadata={
                "reason": "attempted_unbound_callback",
                "presented_client_id": identity.service_client_id,
            },
            commit=True,
        )
        raise HTTPException(
            status_code=403,
            detail="此服務尚未綁定 Service Client Token,請先由管理員於服務註冊表綁定",
        )
    if service.service_client_id != identity.service_client_id:
        log_audit_event(
            db,
            actor=None,
            action="service.audit_callback",
            resource_type="registered_service",
            resource_id=service.id,
            status="denied",
            detail="拒絕 audit callback:presented Service Client Token 與服務綁定不符",
            metadata={
                "reason": "attempted_cross_service_callback",
                "bound_client_id": service.service_client_id,
                "presented_client_id": identity.service_client_id,
            },
            commit=True,
        )
        raise HTTPException(
            status_code=403,
            detail="整合金鑰與此服務的綁定不符",
        )

    body = payload.model_dump(mode="json")
    if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > _AUDIT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="audit callback payload 過大")

    row = launch_mod.record_service_audit_callback(
        db,
        service_id=service.id,
        launch_id=payload.launch_id,
        event_type=payload.event_type,
        payload=body,
        classification_level=payload.classification_level,
        integration_key_id=identity.service_client_id,
    )
    db.flush()
    log_audit_event(
        db,
        actor=None,
        action="service.audit_callback",
        resource_type="registered_service",
        resource_id=service.id,
        detail=f"服務回報 audit 事件 {payload.event_type}",
        metadata={
            "event_type": payload.event_type,
            "launch_id": payload.launch_id,
            "trace_id": payload.trace_id,
            "integration_key_id": identity.service_client_id,
        },
        commit=True,
    )
    db.refresh(row)
    return row


# ── Manifest fetch (doc §4) ─────────────────────────────────────────────────


@router.get("/{service_id}/manifest")
def get_service_manifest(
    service_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    service = _service_or_404(db, service_id)
    try:
        return launch_mod.fetch_service_manifest(service.entry_url)
    except ManifestFetchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ── Project bindings (doc §13) ──────────────────────────────────────────────


@router.get(
    "/{service_id}/project-bindings",
    response_model=list[ProjectBindingResponse],
)
def list_project_bindings(
    service_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = _service_or_404(db, service_id)
    if not can_access_service(db, current_user, service):
        raise HTTPException(status_code=403, detail="無權存取此服務")
    return (
        db.query(ServiceProjectBinding)
        .filter(ServiceProjectBinding.service_id == service.id)
        .order_by(ServiceProjectBinding.created_at)
        .all()
    )


@router.post(
    "/{service_id}/project-bindings",
    response_model=ProjectBindingResponse,
    status_code=201,
)
def create_project_binding(
    service_id: str,
    request: ProjectBindingCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    service = _service_or_404(db, service_id)
    existing = (
        db.query(ServiceProjectBinding)
        .filter(
            ServiceProjectBinding.service_id == service.id,
            ServiceProjectBinding.project_id == request.project_id,
        )
        .first()
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="此服務已綁定該專案")
    binding = ServiceProjectBinding(
        service_id=service.id,
        project_id=request.project_id,
        is_primary_entry=request.is_primary_entry,
        created_by=admin.id,
    )
    db.add(binding)
    db.commit()
    db.refresh(binding)
    log_audit_event(
        db,
        actor=admin,
        action="service.project_bind",
        resource_type="registered_service",
        resource_id=service.id,
        detail=f"服務「{service.name}」綁定專案 {request.project_id}",
        commit=True,
    )
    return binding


@router.delete("/{service_id}/project-bindings/{binding_id}")
def delete_project_binding(
    service_id: str,
    binding_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    service = _service_or_404(db, service_id)
    binding = (
        db.query(ServiceProjectBinding)
        .filter(
            ServiceProjectBinding.id == binding_id,
            ServiceProjectBinding.service_id == service.id,
        )
        .first()
    )
    if binding is None:
        raise HTTPException(status_code=404, detail="綁定不存在")
    db.delete(binding)
    db.commit()
    log_audit_event(
        db,
        actor=admin,
        action="service.project_unbind",
        resource_type="registered_service",
        resource_id=service.id,
        detail=f"服務「{service.name}」解除綁定 {binding.project_id}",
        commit=True,
    )
    return {"message": "綁定已移除"}
