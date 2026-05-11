import httpx
from datetime import datetime, timezone
from anila_core.security import UnsafeEndpointError, validate_outbound_url
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.agent import Agent
from app.models.model_registry import ModelRegistry
from app.models.token_usage import TokenUsage
from app.models.user import User
from app.schemas.model_registry import ModelCreate, ModelUpdate, ModelResponse
from app.services.audit_service import log_audit_event
from app.services.auth_service import (
    get_current_user,
    is_owner,
    require_admin,
    require_owner,
    verify_service_token,
)

router = APIRouter(prefix="/api/models", tags=["模型管理"])

# Sentinel returned to non-owner callers in place of the actual endpoint
# URL. Two variants:
#   <owner-only>  — generic redaction; URL is sensitive deployment topology
#   <internal>    — additional hint that the model lives on internal docker
#                   network and is unreachable from outside the platform stack
# Owner viewers always see the real URL. Non-owners pick variant based on
# the row's ``is_internal`` flag so logs/UI carry intent.
ENDPOINT_REDACTED = "<owner-only>"
ENDPOINT_INTERNAL = "<internal>"


def _enforce_endpoint_url(url: str) -> None:
    """SSRF guard parity with agents.py / ingestion credentials.

    Mirrors agents.py _enforce_endpoint_url. Same env-driven overrides
    apply (ANILA_ALLOW_PRIVATE_ENDPOINT for RFC1918, ANILA_ALLOW_HTTP_ENDPOINT
    for http://, ANILA_TRUSTED_HOSTS comma-list for docker service-name
    short-circuit). Previously model endpoints registered without any guard
    so admins could point at internal services like csp-db; closing that
    parity gap so all three places (agents / ingestion / models) share
    the same validator.
    """
    try:
        validate_outbound_url(url)
    except UnsafeEndpointError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _build_response(model: ModelRegistry, *, caller: User | None = None) -> dict:
    """Serialize a model row.

    ``endpoint_url`` is owner-only — admins and below see
    ``ENDPOINT_REDACTED`` because the URL+port carries deployment-topology
    detail (which GPU box / which container LAN, often the only thing
    standing between a curious admin and direct unmediated access). When
    ``caller`` is None (e.g. internal callers passing through verify_service_token)
    we redact too — opt-in to surface by passing the caller explicitly.
    """
    show_endpoint = caller is not None and is_owner(caller)
    is_internal = bool(getattr(model, "is_internal", False))
    # Redaction sentinel picks the variant that conveys the most intent:
    # internal models → <internal> (lives on anila-models-net, unreachable
    # from outside the platform stack); external → <owner-only> (admin can
    # see the row exists but not the deployment topology).
    if show_endpoint:
        endpoint = model.endpoint_url
    elif is_internal:
        endpoint = ENDPOINT_INTERNAL
    else:
        endpoint = ENDPOINT_REDACTED
    data = {
        "id": model.id,
        "name": model.name,
        "display_name": model.display_name,
        "model_type": model.model_type,
        "endpoint_url": endpoint,
        "api_version": model.api_version,
        "is_active": model.is_active,
        "is_router_primary": bool(model.is_router_primary),
        "health_status": model.health_status,
        "health_checked_at": model.health_checked_at,
        "description": model.description,
        "context_window": model.context_window,
        "base_model_id": model.base_model_id,
        "base_model_name": model.base_model.display_name if model.base_model else None,
        "is_internal": is_internal,
        "created_at": model.created_at,
        "updated_at": model.updated_at,
    }
    return data


@router.get("", response_model=list[ModelResponse])
def list_models(
    model_type: str | None = Query(None, description="篩選模型類型: llm/vlm/embedding/agent"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(ModelRegistry).order_by(ModelRegistry.model_type, ModelRegistry.name)
    if model_type:
        query = query.filter(ModelRegistry.model_type == model_type)
    # Non-admin/owner users only see models they are authorized for. Owner
    # inherits admin's full registry view (auth_service.require_admin
    # covers both, but this list query uses a direct role check so we
    # need to keep the tier explicit here too).
    if current_user.role not in ("admin", "owner"):
        allowed_ids = [m.id for m in current_user.allowed_models]
        if not allowed_ids:
            return []
        query = query.filter(ModelRegistry.id.in_(allowed_ids))
    return [_build_response(m, caller=current_user) for m in query.all()]


@router.post("", response_model=ModelResponse)
def create_model(
    request: ModelCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    existing = db.query(ModelRegistry).filter(ModelRegistry.name == request.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="模型名稱已存在")

    _enforce_endpoint_url(request.endpoint_url)

    # Validate base_model_id if provided
    if request.base_model_id:
        base = db.query(ModelRegistry).filter(ModelRegistry.id == request.base_model_id).first()
        if not base:
            raise HTTPException(status_code=400, detail="底層模型不存在")

    model = ModelRegistry(**request.model_dump())
    db.add(model)
    db.commit()
    db.refresh(model)
    log_audit_event(
        db,
        actor=admin,
        action="create",
        resource_type="model",
        resource_id=model.id,
        detail=f"建立模型「{model.display_name}」",
        commit=True,
    )
    return _build_response(model, caller=admin)


@router.get("/router-primary")
def get_router_primary(
    _: None = Depends(verify_service_token),
    db: Session = Depends(get_db),
):
    """Return the LLM designated as ANILA Router's primary model.

    Service-to-service endpoint consumed by anila-core-router at boot and on
    TTL refresh. Returns 404 when no primary is set so the caller can fall
    back to a clear "no model available" error instead of silently using the
    wrong upstream.
    """
    model = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.is_router_primary.is_(True))
        .first()
    )
    if not model:
        raise HTTPException(status_code=404, detail="尚未指定 ANILA 主路由模型")
    if not model.is_active:
        raise HTTPException(status_code=409, detail="已指定的主路由模型已被停用")
    return {
        "id": model.id,
        "name": model.name,
        "display_name": model.display_name,
        "model_type": model.model_type,
        "endpoint_url": model.endpoint_url,
        "api_version": model.api_version,
        "health_status": model.health_status,
    }


@router.post("/{model_id}/set-router-primary", response_model=ModelResponse)
def set_router_primary(
    model_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Mark a model as ANILA Router's primary LLM (clearing any previous one)."""
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    if model.model_type != "llm":
        raise HTTPException(status_code=400, detail="僅 LLM 類型可設為 ANILA 主路由模型")
    if not model.is_active:
        raise HTTPException(status_code=400, detail="已停用的模型不能設為主路由模型")

    # Clear previous primary first to avoid violating the partial unique index.
    (
        db.query(ModelRegistry)
        .filter(ModelRegistry.is_router_primary.is_(True), ModelRegistry.id != model_id)
        .update({"is_router_primary": False}, synchronize_session=False)
    )
    model.is_router_primary = True
    db.commit()
    db.refresh(model)
    log_audit_event(
        db,
        actor=admin,
        action="set_router_primary",
        resource_type="model",
        resource_id=model.id,
        detail=f"設為 ANILA 主路由模型: {model.display_name}",
        commit=True,
    )
    return _build_response(model, caller=admin)


@router.post("/{model_id}/unset-router-primary", response_model=ModelResponse)
def unset_router_primary(
    model_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Remove the ANILA Router primary designation from a model."""
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    if not model.is_router_primary:
        return _build_response(model, caller=admin)

    model.is_router_primary = False
    db.commit()
    db.refresh(model)
    log_audit_event(
        db,
        actor=admin,
        action="unset_router_primary",
        resource_type="model",
        resource_id=model.id,
        detail=f"取消 ANILA 主路由模型: {model.display_name}",
        commit=True,
    )
    return _build_response(model, caller=admin)


@router.get("/{model_id}", response_model=ModelResponse)
def get_model(
    model_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    if current_user.role not in ("admin", "owner"):
        allowed_ids = {m.id for m in current_user.allowed_models}
        if model.id not in allowed_ids:
            raise HTTPException(status_code=404, detail="模型不存在")
    return _build_response(model, caller=current_user)


@router.put("/{model_id}", response_model=ModelResponse)
def update_model(
    model_id: int,
    request: ModelUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")

    update_data = request.model_dump(exclude_unset=True)

    if "endpoint_url" in update_data and update_data["endpoint_url"] is not None:
        _enforce_endpoint_url(update_data["endpoint_url"])

    # Validate base_model_id if provided
    if "base_model_id" in update_data and update_data["base_model_id"]:
        base = db.query(ModelRegistry).filter(ModelRegistry.id == update_data["base_model_id"]).first()
        if not base:
            raise HTTPException(status_code=400, detail="底層模型不存在")
        if base.id == model_id:
            raise HTTPException(status_code=400, detail="不能將自己設為底層模型")

    for field, value in update_data.items():
        setattr(model, field, value)

    db.commit()
    db.refresh(model)
    log_audit_event(
        db,
        actor=admin,
        action="update",
        resource_type="model",
        resource_id=model.id,
        detail=f"更新模型「{model.display_name}」",
        commit=True,
    )
    return _build_response(model, caller=admin)


def _client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


@router.delete("/{model_id}")
def deactivate_model(
    model_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    model.is_active = False
    # Deactivating a model that's the router primary would leave the router
    # without a primary AND keep is_router_primary=true on a disabled row —
    # which is_active filters in the picker would then trip on. Clear the
    # primary flag here so the invariant ``is_router_primary => is_active``
    # holds; admin must explicitly re-pin a primary after re-activation.
    if model.is_router_primary:
        model.is_router_primary = False
    db.commit()
    log_audit_event(
        db,
        actor=admin,
        action="deactivate",
        resource_type="model",
        resource_id=model.id,
        detail=f"停用模型「{model.display_name}」",
        ip_address=_client_ip(request),
        commit=True,
    )
    return {"message": "模型已停用"}


@router.post("/{model_id}/activate", response_model=ModelResponse)
def activate_model(
    model_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Re-enable a previously deactivated model.

    Symmetric with ``DELETE /{model_id}`` (which sets ``is_active=False``).
    The router primary flag is NOT auto-restored — admins should pin a
    primary explicitly via ``POST /{model_id}/set-router-primary`` after
    re-activation.
    """
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    if model.is_active:
        return _build_response(model, caller=admin)
    model.is_active = True
    db.commit()
    db.refresh(model)
    log_audit_event(
        db,
        actor=admin,
        action="activate",
        resource_type="model",
        resource_id=model.id,
        detail=f"啟用模型「{model.display_name}」",
        ip_address=_client_ip(request),
        commit=True,
    )
    return _build_response(model, caller=admin)


@router.delete("/{model_id}/purge")
def purge_model(
    model_id: int,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    """Hard-delete a model. Owner-only — irreversible. Blocked if
    token_usage or other models reference it."""
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")

    usage_count = db.query(TokenUsage).filter(TokenUsage.model_id == model_id).count()
    if usage_count > 0:
        raise HTTPException(
            status_code=409,
            detail=f"此模型尚有 {usage_count} 筆用量紀錄，無法硬刪除。請改以「停用」保留歷史",
        )

    dependent_count = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.base_model_id == model_id)
        .count()
    )
    if dependent_count > 0:
        raise HTTPException(
            status_code=409,
            detail=f"尚有 {dependent_count} 個模型以此為底層模型，請先解除關聯",
        )

    display_name = model.display_name
    # agents.base_model_id will be set NULL via ondelete="SET NULL"
    # user_allowed_models / api_key_allowed_models cascade delete
    db.delete(model)
    db.commit()
    log_audit_event(
        db,
        actor=owner,
        action="delete",
        resource_type="model",
        resource_id=model_id,
        detail=f"刪除模型「{display_name}」",
        commit=True,
    )
    return {"message": f"已刪除模型「{display_name}」"}


@router.post("/{model_id}/health-check")
async def trigger_health_check(
    model_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")

    ip = _client_ip(request)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Try common health endpoints
            for path in ["/health", "/v1/models", "/"]:
                try:
                    resp = await client.get(f"{model.endpoint_url.rstrip('/')}{path}")
                    if resp.status_code < 500:
                        model.health_status = "online"
                        model.health_checked_at = datetime.now(timezone.utc)
                        db.commit()
                        log_audit_event(
                            db,
                            actor=admin,
                            action="health_check",
                            resource_type="model",
                            resource_id=model.id,
                            detail=f"手動健康檢查成功: {model.display_name}",
                            ip_address=ip,
                            commit=True,
                        )
                        return {"status": "online", "detail": f"端點 {path} 回應正常"}
                except httpx.ConnectError:
                    continue

            model.health_status = "offline"
            model.health_checked_at = datetime.now(timezone.utc)
            db.commit()
            log_audit_event(
                db,
                actor=admin,
                action="health_check",
                resource_type="model",
                resource_id=model.id,
                detail=f"手動健康檢查離線: {model.display_name}",
                ip_address=ip,
                commit=True,
            )
            return {"status": "offline", "detail": "無法連線到模型端點"}
    except Exception as e:
        model.health_status = "offline"
        model.health_checked_at = datetime.now(timezone.utc)
        db.commit()
        log_audit_event(
            db,
            actor=admin,
            action="health_check",
            resource_type="model",
            resource_id=model.id,
            status="failure",
            detail=f"手動健康檢查失敗: {model.display_name} ({e})",
            ip_address=ip,
            commit=True,
        )
        return {"status": "offline", "detail": str(e)}
