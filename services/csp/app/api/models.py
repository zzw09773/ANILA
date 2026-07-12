from datetime import datetime, timezone
from anila_security import UnsafeEndpointError, validate_outbound_url
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
from app.database import get_db
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
from app.services.health_checker import (
    HEALTH_UNHEALTHY,
    normalize_health_status,
    probe_model_health_detailed,
)
from app.services.service_token_envelope import encode_service_token_envelope

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
    for http://, ANILA_TRUSTED_HOSTS comma-list + DB-backed admin allow-list
    via trusted_host_service). Previously model endpoints registered without
    any guard so admins could point at internal services like csp-db; closing
    that parity gap so all three places (agents / ingestion / models) share
    the same validator.

    When the failure is fixable (single-label hostname or internal-zone
    suffix — i.e. admin could legitimately want this host trusted), we
    surface a *structured* detail dict instead of a plain message string,
    so the frontend can render an actionable confirm modal ("Add
    'foobar' to trusted hosts?") instead of just an opaque alert. Other
    failure reasons (loopback / metadata / private IP) keep the plain
    string detail — those aren't safe to bypass via the UI.

    Slice 6a (doc 04 §8): validated with ``endpoint_kind="model"`` so the
    production HTTPS invariant applies — a production deployment
    (``ANILA_ENV=production``) rejects http:// model endpoints even with
    ``ANILA_ALLOW_HTTP_ENDPOINT=1`` (fail-closed, no flag bypass).
    """
    try:
        validate_outbound_url(url, endpoint_kind="model")
    except UnsafeEndpointError as exc:
        if exc.fixable_by_trust_host:
            detail = {
                "code": "untrusted_host",
                "host": exc.host,
                "reason": exc.reason,
                "message": str(exc),
                "hint": (
                    f"hostname {exc.host!r} 不在受信任清單。"
                    f"若該主機在內部 docker network 上(例如 anila-models-net "
                    f"內的推論服務),管理員可在 /trusted-hosts 加入後再試。"
                ),
            }
        else:
            detail = str(exc)
        raise HTTPException(status_code=400, detail=detail) from exc


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
        "is_image_primary": bool(getattr(model, "is_image_primary", False)),
        # Slice 6a: always surface the five-state vocabulary; 'disabled' when
        # inactive, legacy online/connecting/offline normalized on read.
        "health_status": normalize_health_status(
            model.health_status, is_active=bool(model.is_active)
        ),
        "health_checked_at": model.health_checked_at,
        "description": model.description,
        "context_window": model.context_window,
        "base_model_id": model.base_model_id,
        "base_model_name": model.base_model.display_name if model.base_model else None,
        "is_internal": is_internal,
        # Slice 6a (doc 04 §2/§3): ModelEndpoint formalized fields. The
        # per-model key is exposed ONLY as a boolean presence flag — never the
        # ciphertext / secret ref, never plaintext.
        "protocol": getattr(model, "protocol", "openai_compatible") or "openai_compatible",
        "classification_ceiling": getattr(model, "classification_ceiling", None),
        "owner_department_id": getattr(model, "owner_department_id", None),
        "supports_streaming": bool(getattr(model, "supports_streaming", True)),
        "supports_json_schema": bool(getattr(model, "supports_json_schema", False)),
        "supports_tools": bool(getattr(model, "supports_tools", False)),
        "has_api_key": bool(getattr(model, "api_key_secret_ref", None)),
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

    # Slice 6a (doc 04 §3): api_key is write-only — encrypt into the
    # ``enc::v1::`` envelope and store as api_key_secret_ref; never a column
    # by itself, so pop it before constructing the row.
    data = request.model_dump()
    api_key = (data.pop("api_key", None) or "").strip()
    model = ModelRegistry(**data)
    if api_key:
        model.api_key_secret_ref = encode_service_token_envelope(api_key)
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


@router.get("/image-primary")
def get_image_primary(
    _: None = Depends(verify_service_token),
    db: Session = Depends(get_db),
):
    """Return the model designated as the primary image (FLUX) model.

    Service-to-service endpoint consumed by flux2-dev-agent / anila-studio
    on a 60s TTL refresh (doc 2026-07-06-flux-image-primary-design.md §1).
    Returns 404 when no primary is set so callers fall back to their env
    config instead of silently using the wrong endpoint. Never returns the
    model's API key — key management stays with each consumer's own env.
    """
    model = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.is_image_primary.is_(True))
        .first()
    )
    if not model:
        raise HTTPException(status_code=404, detail="尚未指定主圖像模型")
    if not model.is_active:
        raise HTTPException(status_code=409, detail="已指定的主圖像模型已被停用")
    return {
        "id": model.id,
        "name": model.name,
        "display_name": model.display_name,
        "model_type": model.model_type,
        "endpoint_url": model.endpoint_url,
        "api_version": model.api_version,
        "health_status": model.health_status,
    }


@router.post("/{model_id}/set-image-primary", response_model=ModelResponse)
def set_image_primary(
    model_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Mark a model as the primary image (FLUX) model (clearing any previous one)."""
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    if model.model_type != "image":
        raise HTTPException(status_code=400, detail="僅 image 類型可設為主圖像模型")
    if not model.is_active:
        raise HTTPException(status_code=400, detail="已停用的模型不能設為主圖像模型")

    # Clear previous primary first to avoid violating the partial unique index.
    (
        db.query(ModelRegistry)
        .filter(ModelRegistry.is_image_primary.is_(True), ModelRegistry.id != model_id)
        .update({"is_image_primary": False}, synchronize_session=False)
    )
    model.is_image_primary = True
    db.commit()
    db.refresh(model)
    log_audit_event(
        db,
        actor=admin,
        action="set_image_primary",
        resource_type="model",
        resource_id=model.id,
        detail=f"設為主圖像模型: {model.display_name}",
        commit=True,
    )
    return _build_response(model, caller=admin)


@router.post("/{model_id}/unset-image-primary", response_model=ModelResponse)
def unset_image_primary(
    model_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Remove the primary image-model designation from a model."""
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    if not model.is_image_primary:
        return _build_response(model, caller=admin)

    model.is_image_primary = False
    db.commit()
    db.refresh(model)
    log_audit_event(
        db,
        actor=admin,
        action="unset_image_primary",
        resource_type="model",
        resource_id=model.id,
        detail=f"取消主圖像模型: {model.display_name}",
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

    # Slice 6a (doc 04 §3): api_key is write-only. When supplied non-empty,
    # re-encrypt into api_key_secret_ref; it is never assigned as a column.
    api_key = update_data.pop("api_key", None)
    if api_key is not None and str(api_key).strip():
        model.api_key_secret_ref = encode_service_token_envelope(str(api_key).strip())

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
    # Same invariant for the image-primary flag (doc
    # 2026-07-06-flux-image-primary-design.md §1): a disabled row must not
    # stay pinned as primary.
    if model.is_image_primary:
        model.is_image_primary = False
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


async def _probe_and_persist(model: ModelRegistry, admin: User, db: Session, ip: str | None) -> dict:
    """Slice 6a: shared active-probe path for POST /test and the legacy alias.

    Runs the five-state probe (SSRF re-validated inside), persists the new
    ``health_status`` (five-state) + ``health_checked_at`` and audits the
    result. Returns ``{status, last_checked, latency_ms}``. The probe carries
    NO real user data (doc 04 §9).
    """
    status, latency_ms = await probe_model_health_detailed(model.endpoint_url)
    checked_at = datetime.now(timezone.utc)
    model.health_status = status
    model.health_checked_at = checked_at
    db.commit()
    log_audit_event(
        db,
        actor=admin,
        action="health_check",
        resource_type="model",
        resource_id=model.id,
        status=("failure" if status == HEALTH_UNHEALTHY else "success"),
        detail=f"主動健康檢查: {model.display_name} → {status} ({latency_ms}ms)",
        ip_address=ip,
        commit=True,
    )
    return {
        "status": status,
        "last_checked": checked_at,
        "latency_ms": latency_ms,
    }


@router.get("/{model_id}/health")
def get_model_health(
    model_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Slice 6a (doc 04 §9): current five-state health without probing.

    Passive read — returns the stored status normalized to the five-state
    vocabulary (``disabled`` when inactive), plus ``last_checked`` and
    ``latency`` (null here — latency is measured only by an active
    POST /test). Non-admin/owner callers only see models they are permitted
    to use (same gate as GET /{model_id}).
    """
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    if current_user.role not in ("admin", "owner"):
        allowed_ids = {m.id for m in current_user.allowed_models}
        if model.id not in allowed_ids:
            raise HTTPException(status_code=404, detail="模型不存在")
    return {
        "id": model.id,
        "status": normalize_health_status(
            model.health_status, is_active=bool(model.is_active)
        ),
        "last_checked": model.health_checked_at,
        "latency_ms": None,
    }


@router.post("/{model_id}/test")
async def test_model(
    model_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Slice 6a (doc 04 §9): active probe — updates status + returns latency.

    Admin-only. Runs the five-state health probe against the endpoint,
    persists the result and returns ``{status, last_checked, latency_ms}``.
    """
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    return await _probe_and_persist(model, admin, db, _client_ip(request))


@router.post("/{model_id}/health-check")
async def trigger_health_check(
    model_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """DEPRECATED legacy alias for POST /{model_id}/test.

    Kept working for existing admin UIs; converged onto the same five-state
    probe. Response carries ``deprecated: true`` + a pointer to the new route.
    """
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    result = await _probe_and_persist(model, admin, db, _client_ip(request))
    result["deprecated"] = True
    result["detail"] = "此路由已棄用,請改用 POST /api/models/{id}/test"
    return result
