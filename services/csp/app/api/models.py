from collections.abc import Mapping
from datetime import datetime, timezone
import ipaddress
from typing import Any

from anila_security import (
    ModelGovernanceError,
    ProviderLocality,
    TransportTarget,
    UnsafeEndpointError,
    validate_outbound_url,
)
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
from anila_contracts import Classification as ClassificationLevel
from app.database import get_db
from app.models.model_registry import ModelRegistry
from app.models.token_usage import TokenUsage
from app.models.user import User
from app.schemas.model_registry import (
    ModelCreate,
    ModelResponse,
    ModelUpdate,
    validate_provider_snapshot_fields,
)
from app.services.audit_service import log_audit_event
from app.services.model_governance_receipts import admit_registry_provider
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

_SNAPSHOT_FIELDS = {
    "provider_locality",
    "transport_target",
    "transport_target_sha256",
    "model_registry_revision",
    "upstream_provider_locality",
    "upstream_transport_target",
    "upstream_transport_target_sha256",
    "egress_policy_id",
    "upstream_egress_policy_id",
}


def _require_provider_authority(model: ModelRegistry) -> None:
    """Fail closed in formal Gate 5 posture before enabling or probing a row."""

    try:
        admit_registry_provider(model)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="模型 provider authority 驗證失敗，操作已拒絕",
        ) from exc


def _lock_model_row(db: Session, model_id: int) -> ModelRegistry | None:
    """Load one registry row with a mutation lock in the caller transaction."""

    return (
        db.query(ModelRegistry)
        .filter(ModelRegistry.id == model_id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )


def _snapshot_locality(value: Any) -> str:
    raw = value or ProviderLocality.UNCLASSIFIED.value
    if isinstance(raw, ProviderLocality):
        return raw.value
    return str(raw)


def _parse_target(value: Any, *, locality: str | None, field: str) -> TransportTarget | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        target = TransportTarget.from_dict(value, field=field)
    elif isinstance(value, str):
        dns_policy = (
            "production_fail_closed"
            if locality == ProviderLocality.EXTERNAL_GOVERNED.value
            else "none"
        )
        target = TransportTarget.parse(value, dns_policy=dns_policy, field=field)
    else:
        raise ModelGovernanceError(f"{field} must be a transport target string or object")
    if locality == ProviderLocality.EXTERNAL_GOVERNED.value:
        try:
            ipaddress.ip_address(target.host)
        except ValueError:
            pass
        else:
            if target.dns_policy != "none":
                target = TransportTarget(
                    target.scheme,
                    target.host,
                    target.port,
                    target.port_mode,
                    target.path,
                    "none",
                )
    return target


def _endpoint_target(endpoint_url: str, locality: str) -> TransportTarget:
    """Parse the compatibility endpoint using the locality's DNS policy."""

    dns_policy = (
        "production_fail_closed"
        if locality == ProviderLocality.EXTERNAL_GOVERNED.value
        else "none"
    )
    target = TransportTarget.parse(
        endpoint_url,
        dns_policy=dns_policy,
        field="endpoint_url",
    )
    normalized = _parse_target(target.to_dict(), locality=locality, field="endpoint_url")
    if normalized is None:  # pragma: no cover - defensive type guard
        raise ModelGovernanceError("endpoint_url must be a transport target")
    return normalized


def _next_provider_revision(previous: str | None) -> str:
    if previous is None or not str(previous).strip():
        return "1"
    raw = str(previous).strip()
    if raw.isdigit():
        return str(int(raw) + 1)
    # Preserve opaque operator-supplied revisions while making every rebuilt
    # snapshot distinct and auditable.
    if raw.endswith(":1"):
        return raw[:-2] + ":2"
    return f"{raw}:1"


def _provider_is_internal(locality: str, legacy_value: Any) -> bool:
    if locality in {
        ProviderLocality.INTERNAL_ISOLATED.value,
        ProviderLocality.INTERNAL_SHIM.value,
    }:
        return True
    if locality == ProviderLocality.EXTERNAL_GOVERNED.value:
        return False
    return bool(legacy_value)


def _snapshot_values(
    *,
    endpoint_url: str,
    provider_locality: str,
    transport_target: Any,
    transport_target_sha256: str | None,
    model_registry_revision: str | None,
    upstream_provider_locality: str | None,
    upstream_transport_target: Any,
    upstream_transport_target_sha256: str | None,
    egress_policy_id: str | None,
    upstream_egress_policy_id: str | None,
    previous_revision: str | None = None,
    snapshot_changed: bool = True,
) -> dict[str, Any]:
    """Build one complete canonical provider snapshot for a row.

    The compatibility ``endpoint_url`` is normalized to the same canonical
    target string whenever a snapshot is present.  Unclassified legacy rows
    may intentionally have no target snapshot.
    """

    locality = _snapshot_locality(provider_locality)
    # Classified rows must have a complete target.  If the caller did not send
    # the new field, derive it from endpoint_url; this keeps old clients
    # compatible without promoting is_internal to policy.
    supplied_target = _parse_target(
        transport_target,
        locality=locality,
        field="transport_target",
    )
    target = supplied_target
    if target is None and locality != ProviderLocality.UNCLASSIFIED.value:
        target = _endpoint_target(endpoint_url, locality)

    if target is None:
        # An unclassified row is allowed to remain a legacy compatibility row.
        validate_provider_snapshot_fields(
            provider_locality=locality,
            transport_target=None,
            transport_target_sha256=None,
            upstream_provider_locality=None,
            upstream_transport_target=None,
            upstream_transport_target_sha256=None,
            egress_policy_id=None,
            upstream_egress_policy_id=None,
        )
        return {
            "endpoint_url": endpoint_url,
            "provider_locality": locality,
            "transport_target": None,
            "transport_target_sha256": None,
            "model_registry_revision": None,
            "upstream_provider_locality": None,
            "upstream_transport_target": None,
            "upstream_transport_target_sha256": None,
            "egress_policy_id": None,
            "upstream_egress_policy_id": None,
            "is_internal": _provider_is_internal(locality, False),
        }

    endpoint_target = _endpoint_target(endpoint_url, locality)
    if endpoint_target.canonical != target.canonical:
        raise ModelGovernanceError(
            "transport_target canonical value must equal endpoint_url"
        )

    upstream = _parse_target(
        upstream_transport_target,
        locality=upstream_provider_locality,
        field="upstream_transport_target",
    )
    target_dict = target.to_dict()
    upstream_dict = None if upstream is None else upstream.to_dict()
    validate_provider_snapshot_fields(
        provider_locality=locality,
        transport_target=target_dict,
        transport_target_sha256=transport_target_sha256,
        upstream_provider_locality=upstream_provider_locality,
        upstream_transport_target=upstream_dict,
        upstream_transport_target_sha256=upstream_transport_target_sha256,
        egress_policy_id=egress_policy_id,
        upstream_egress_policy_id=upstream_egress_policy_id,
        allow_incomplete_unclassified=False,
    )
    if snapshot_changed:
        revision = (
            _next_provider_revision(previous_revision)
            if previous_revision is not None
            else model_registry_revision or "1"
        )
    else:
        revision = previous_revision or model_registry_revision or "1"
    return {
        "endpoint_url": target.canonical,
        "provider_locality": locality,
        "transport_target": target_dict,
        "transport_target_sha256": target.sha256,
        "model_registry_revision": revision,
        "upstream_provider_locality": upstream_provider_locality,
        "upstream_transport_target": upstream_dict,
        "upstream_transport_target_sha256": (
            None if upstream is None else upstream.sha256
        ),
        "egress_policy_id": egress_policy_id,
        "upstream_egress_policy_id": upstream_egress_policy_id,
        "is_internal": _provider_is_internal(locality, False),
    }


def _prepare_provider_snapshot(
    *,
    endpoint_url: str,
    provider_locality: str,
    transport_target: Any,
    transport_target_sha256: str | None,
    model_registry_revision: str | None,
    upstream_provider_locality: str | None,
    upstream_transport_target: Any,
    upstream_transport_target_sha256: str | None,
    egress_policy_id: str | None,
    upstream_egress_policy_id: str | None,
    previous_revision: str | None = None,
    snapshot_changed: bool = True,
    legacy_is_internal: Any = False,
) -> dict[str, Any]:
    try:
        values = _snapshot_values(
            endpoint_url=endpoint_url,
            provider_locality=provider_locality,
            transport_target=transport_target,
            transport_target_sha256=transport_target_sha256,
            model_registry_revision=model_registry_revision,
            upstream_provider_locality=upstream_provider_locality,
            upstream_transport_target=upstream_transport_target,
            upstream_transport_target_sha256=upstream_transport_target_sha256,
            egress_policy_id=egress_policy_id,
            upstream_egress_policy_id=upstream_egress_policy_id,
            previous_revision=previous_revision,
            snapshot_changed=snapshot_changed,
        )
    except ModelGovernanceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if values["provider_locality"] == ProviderLocality.UNCLASSIFIED.value:
        values["is_internal"] = _provider_is_internal(
            values["provider_locality"], legacy_is_internal
        )
    return values


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

    Provider snapshot fields and egress policy IDs are client-supplied
    metadata, not verified authority.  Until server-side signed authority is
    wired, they must never bypass this shared URL guard.
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
    provider_locality = _snapshot_locality(
        getattr(model, "provider_locality", ProviderLocality.UNCLASSIFIED.value)
    )
    is_internal = _provider_is_internal(
        provider_locality,
        getattr(model, "is_internal", False),
    )
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
    raw_ceiling = getattr(model, "classification_ceiling", None)
    if not isinstance(raw_ceiling, str):
        raise RuntimeError(
            "model_registry.classification_ceiling must be a non-null canonical value"
        )
    classification_ceiling = ClassificationLevel.from_storage(
        raw_ceiling
    ).to_storage()
    provider_snapshot = {
        "transport_target": getattr(model, "transport_target", None),
        "transport_target_sha256": getattr(model, "transport_target_sha256", None),
        "model_registry_revision": getattr(model, "model_registry_revision", None),
        "upstream_provider_locality": getattr(
            model, "upstream_provider_locality", None
        ),
        "upstream_transport_target": getattr(
            model, "upstream_transport_target", None
        ),
        "upstream_transport_target_sha256": getattr(
            model, "upstream_transport_target_sha256", None
        ),
        "egress_policy_id": getattr(model, "egress_policy_id", None),
        "upstream_egress_policy_id": getattr(
            model, "upstream_egress_policy_id", None
        ),
    }
    if not show_endpoint:
        # Provider transport, hashes, revisions and egress identifiers are
        # deployment authority material, not ordinary model metadata.  Match
        # endpoint redaction for every non-owner response, including service
        # callers that pass no user object.
        provider_snapshot = {field: None for field in provider_snapshot}

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
        "provider_locality": provider_locality,
        **provider_snapshot,
        # Slice 6a (doc 04 §2/§3): ModelEndpoint formalized fields. The
        # per-model key is exposed ONLY as a boolean presence flag — never the
        # ciphertext / secret ref, never plaintext.
        "protocol": getattr(model, "protocol", "openai_compatible") or "openai_compatible",
        "classification_ceiling": classification_ceiling,
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

    # Validate base_model_id if provided
    if request.base_model_id:
        base = db.query(ModelRegistry).filter(ModelRegistry.id == request.base_model_id).first()
        if not base:
            raise HTTPException(status_code=400, detail="底層模型不存在")

    snapshot = _prepare_provider_snapshot(
        endpoint_url=request.endpoint_url,
        provider_locality=request.provider_locality,
        transport_target=request.transport_target,
        transport_target_sha256=request.transport_target_sha256,
        model_registry_revision=request.model_registry_revision,
        upstream_provider_locality=request.upstream_provider_locality,
        upstream_transport_target=request.upstream_transport_target,
        upstream_transport_target_sha256=request.upstream_transport_target_sha256,
        egress_policy_id=request.egress_policy_id,
        upstream_egress_policy_id=request.upstream_egress_policy_id,
        legacy_is_internal=request.is_internal,
    )
    _enforce_endpoint_url(request.endpoint_url)

    # Slice 6a (doc 04 §3): api_key is write-only — encrypt into the
    # ``enc::v1::`` envelope and store as api_key_secret_ref; never a column
    # by itself, so pop it before constructing the row.
    data = request.model_dump()
    data["classification_ceiling"] = request.classification_ceiling.to_storage()
    api_key = (data.pop("api_key", None) or "").strip()
    for field in _SNAPSHOT_FIELDS:
        data.pop(field, None)
    data.update(snapshot)
    model = ModelRegistry(**data)
    if api_key:
        model.api_key_secret_ref = encode_service_token_envelope(api_key)
    db.add(model)
    # Registration is the first formal admission boundary.  Flush so the
    # server-assigned registry id participates in the exact signed-provider
    # match, but do not commit an active row until that match succeeds.
    try:
        db.flush()
        if model.is_active:
            _require_provider_authority(model)
    except Exception:
        db.rollback()
        raise
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
    model = _lock_model_row(db, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    if model.model_type != "llm":
        raise HTTPException(status_code=400, detail="僅 LLM 類型可設為 ANILA 主路由模型")
    if not model.is_active:
        raise HTTPException(status_code=400, detail="已停用的模型不能設為主路由模型")
    try:
        _require_provider_authority(model)

        # Clear previous primary first to avoid violating the partial unique index.
        (
            db.query(ModelRegistry)
            .filter(ModelRegistry.is_router_primary.is_(True), ModelRegistry.id != model_id)
            .update({"is_router_primary": False}, synchronize_session=False)
        )
        model.is_router_primary = True
        db.commit()
    except Exception:
        db.rollback()
        raise
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
    # The primary flag is only a selector.  Re-admit the locked registry row
    # against the current signed provider authority on every consumer fetch;
    # this closes the window where a cached endpoint survives profile
    # rotation/revocation and would otherwise be handed to a downstream
    # image client.
    _require_provider_authority(model)
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
    _require_provider_authority(model)

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
    model = _lock_model_row(db, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")

    update_data = request.model_dump(exclude_unset=True)
    if "classification_ceiling" in update_data:
        update_data["classification_ceiling"] = update_data[
            "classification_ceiling"
        ].to_storage()

    candidate_endpoint = update_data.get("endpoint_url", model.endpoint_url)
    if candidate_endpoint is None:
        raise HTTPException(status_code=400, detail="endpoint_url 不得為空")
    current_locality = _snapshot_locality(
        getattr(model, "provider_locality", ProviderLocality.UNCLASSIFIED.value)
    )
    candidate_locality = _snapshot_locality(
        update_data.get("provider_locality", current_locality)
    )
    locality_changed = candidate_locality != current_locality
    snapshot_changed = bool(
        _SNAPSHOT_FIELDS.intersection(update_data)
        or "endpoint_url" in update_data
        or locality_changed
    )

    # Omitted fields retain the existing snapshot unless locality changes. A
    # locality transition starts a fresh snapshot; callers must provide the
    # new shim upstream when switching into internal_shim.
    if "transport_target" in update_data:
        candidate_target = update_data["transport_target"]
    elif (
        (locality_changed or "endpoint_url" in update_data)
        and candidate_locality == ProviderLocality.UNCLASSIFIED.value
    ):
        candidate_target = None
    elif locality_changed or "endpoint_url" in update_data:
        # Re-derive a classified target from the new compatibility endpoint;
        # retaining the old target would leave a mixed old/new snapshot.
        candidate_target = None
    else:
        candidate_target = getattr(model, "transport_target", None)

    if "upstream_provider_locality" in update_data:
        candidate_upstream_locality = update_data["upstream_provider_locality"]
    elif locality_changed:
        candidate_upstream_locality = None
    else:
        candidate_upstream_locality = getattr(model, "upstream_provider_locality", None)

    if "upstream_transport_target" in update_data:
        candidate_upstream_target = update_data["upstream_transport_target"]
    elif locality_changed:
        candidate_upstream_target = None
    else:
        candidate_upstream_target = getattr(model, "upstream_transport_target", None)

    def _candidate_value(field: str) -> Any:
        if field in update_data:
            return update_data[field]
        return getattr(model, field, None)

    candidate_target_hash = (
        update_data.get("transport_target_sha256")
        if "transport_target_sha256" in update_data
        else (
            None
            if "transport_target" in update_data
            or "endpoint_url" in update_data
            or locality_changed
            else getattr(model, "transport_target_sha256", None)
        )
    )
    candidate_upstream_hash = (
        update_data.get("upstream_transport_target_sha256")
        if "upstream_transport_target_sha256" in update_data
        else (
            None
            if "upstream_transport_target" in update_data
            or "upstream_provider_locality" in update_data
            or locality_changed
            else getattr(model, "upstream_transport_target_sha256", None)
        )
    )

    # Explicit hash values are accepted for compatibility but are always
    # recomputed from the canonical target before persistence. Clearing a
    # target therefore also clears its hash atomically.
    try:
        snapshot = _prepare_provider_snapshot(
            endpoint_url=candidate_endpoint,
            provider_locality=candidate_locality,
            transport_target=candidate_target,
            transport_target_sha256=candidate_target_hash,
            model_registry_revision=_candidate_value("model_registry_revision"),
            upstream_provider_locality=candidate_upstream_locality,
            upstream_transport_target=candidate_upstream_target,
            upstream_transport_target_sha256=candidate_upstream_hash,
            egress_policy_id=(
                _candidate_value("egress_policy_id")
                if not locality_changed
                else update_data.get("egress_policy_id")
            ),
            upstream_egress_policy_id=(
                _candidate_value("upstream_egress_policy_id")
                if not locality_changed
                else update_data.get("upstream_egress_policy_id")
            ),
            previous_revision=getattr(model, "model_registry_revision", None),
            snapshot_changed=snapshot_changed,
            legacy_is_internal=getattr(model, "is_internal", False),
        )
    except HTTPException:
        raise

    _enforce_endpoint_url(candidate_endpoint)

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

    for field in _SNAPSHOT_FIELDS:
        update_data.pop(field, None)
    update_data.pop("endpoint_url", None)
    # ``is_internal`` is projection-only for classified rows; retain the
    # legacy value only while a row remains unclassified.
    update_data.pop("is_internal", None)
    for field, value in update_data.items():
        setattr(model, field, value)

    for field, value in snapshot.items():
        if field == "is_internal" and snapshot["provider_locality"] == ProviderLocality.UNCLASSIFIED.value:
            # The helper used the old value for unclassified rows; an explicit
            # legacy is_internal PATCH remains authoritative for compatibility.
            value = request.is_internal if "is_internal" in request.model_fields_set else value
        setattr(model, field, value)

    try:
        # The row lock remains held while the final active status and the
        # complete provider snapshot are validated.  A concurrent deactivate
        # or endpoint replacement therefore cannot race this authority check.
        if model.is_active:
            _require_provider_authority(model)
        db.commit()
    except Exception:
        db.rollback()
        raise
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
    model = _lock_model_row(db, model_id)
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
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
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
    model = _lock_model_row(db, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    try:
        if model.is_active:
            _require_provider_authority(model)
            return _build_response(model, caller=admin)
        # Validate the locked, still-inactive row before changing its status.
        # The same transaction then commits the authority decision and
        # activation.
        _require_provider_authority(model)
        model.is_active = True
        db.commit()
    except Exception:
        db.rollback()
        raise
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
    _require_provider_authority(model)
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
