from datetime import datetime, timezone
import asyncio
import json
import logging
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx
from anila_core.security import UnsafeEndpointError, validate_outbound_url, ENDPOINT_KIND_MODEL
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.model_registry import ModelRegistry
from app.models.token_usage import TokenUsage
from app.models.user import User
from app.schemas.contracts.classification import ClassificationLevel
from app.schemas.model_registry import (
    ALLOWED_PROTOCOLS,
    ModelBulkActivateCreatedRequest,
    ModelBulkActivateCreatedResponse,
    ModelBulkImportCreated,
    ModelBulkImportMissing,
    ModelBulkImportRequest,
    ModelBulkImportResponse,
    ModelBulkImportSkipped,
    ModelBulkImportUnchanged,
    ModelCreate,
    ModelUpdate,
    ModelResponse,
)
from app.services import agent_credential_service
from app.services.audit_service import log_audit_event
from app.services.auth_service import (
    get_current_user,
    is_admin_tier,
    require_admin,
    require_owner,
    security,
    verify_service_token,
)
from app.api._service_principal import require_admitted_service_principal
from app.utils.client_ip import client_ip as _client_ip
from app.services.endpoint_author_service import (
    ENDPOINT_INTERNAL,
    ENDPOINT_REDACTED,
    can_see_endpoint_address,
    can_set_endpoint_address,
    require_endpoint_address_author,
    visible_endpoint_url,
)
from app.services.health_checker import (
    HEALTH_UNHEALTHY,
    normalize_health_status,
    probe_model_health_detailed,
)
from app.services.proxy.headers import resolve_model_gateway_key
from app.services.proxy.service import ProxyTuning, resolve_proxy_tuning
from app.services.proxy.urls import join_upstream_path
from app.services.service_token_envelope import (
    decode_service_token_envelope,
    encode_service_token_envelope,
)
from app.services.thinking_probe import PROBEABLE_LEVELS, probe_thinking_effort

logger = logging.getLogger(__name__)

# Columns that bulk-import may fill on an *existing* row when (and only when)
# the stored value is genuinely absent (NULL / empty) AND the existing row
# shares the source endpoint. Admin-edited values are never overwritten.
_IMPORT_FILLABLE_ABSENT = ("description",)

# Endpoint-scoped facts copied from the source registry row onto every newly
# imported model. Driven as the single source of inheritance — do not also
# hand-write the same field list beside this tuple. Per-model facts
# (context_window, supports_*) are NOT here: they stay at ModelCreate defaults
# unless the upstream listing supplies them.
_IMPORT_INHERITED_FROM_SOURCE = (
    "endpoint_url",
    "api_version",
    "protocol",
    "is_internal",
    "classification_ceiling",
    "owner_department_id",
)

# Same-gateway credential envelope (not a ModelCreate field) — copied after
# construct; never plaintext.
_IMPORT_COPY_SECRET_REF = "api_key_secret_ref"

# Explicit caps (batch-approve precedent: input limit + effective bound).
_BULK_IMPORT_ENTRY_CAP = 500
_BULK_IMPORT_BODY_MAX_BYTES = 1_048_576  # 1 MiB upstream body
_BULK_IMPORT_AUDIT_NAME_SAMPLE = 40
_BULK_ACTIVATE_INPUT_LIMIT = 500

router = APIRouter(prefix="/api/models", tags=["模型管理"])

# ENDPOINT_REDACTED / ENDPOINT_INTERNAL are imported from
# endpoint_author_service (single source) and re-exported for tests.


def _caller_is_designated_author_of(
    db: Session, caller: User, model: ModelRegistry
) -> bool:
    """Same authorship question for read visibility and address write.

    A designated endpoint author may list/fetch/set-address only on
    rows they created. Designation alone never reaches another author's
    row — that would let a developer retarget models they cannot see
    (including the router primary) and would make write success differ
    from read not-found as an id-enumeration signal.
    """
    return (
        model.created_by_user_id == caller.id
        and can_set_endpoint_address(db, caller)
    )


def _caller_may_view_model(
    db: Session, caller: User, model: ModelRegistry
) -> bool:
    """Admin-tier sees the registry; others see assigned or authored rows.

    Designated endpoint authors list/fetch rows they created via
    authorship — never via ``user_model_permissions``. Creating a
    registry row must not grant inference access as a side effect, and
    an administrator rewriting allowed models must not strip the
    author's view of gateways they registered.
    """
    if caller.role in ("admin", "owner"):
        return True
    if any(m.id == model.id for m in caller.allowed_models):
        return True
    if _caller_is_designated_author_of(db, caller, model):
        return True
    return False


def _enforce_protocol_endpoint(protocol: str, endpoint_url: str) -> None:
    """Refuse protocol/URL pairs that cannot work.

    ``triton_grpc`` requires ``grpc://`` / ``grpcs://`` (no HTTP path join).
    ``openai_compatible`` requires ``http://`` / ``https://``.
    """
    scheme = (urlparse((endpoint_url or "").strip()).scheme or "").lower()
    if protocol == "triton_grpc":
        if scheme not in ("grpc", "grpcs"):
            raise HTTPException(
                status_code=422,
                detail=(
                    "protocol=triton_grpc 的端點必須為 grpc:// 或 grpcs:// "
                    "（例如 grpc://host:9001），不可填 http(s) URL"
                ),
            )
        return
    if protocol == "openai_compatible":
        if scheme not in ("http", "https"):
            raise HTTPException(
                status_code=422,
                detail=(
                    "protocol=openai_compatible 的端點必須為 http:// 或 https://"
                ),
            )


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

    Slice 6a: validated with ``endpoint_kind="model"`` — http:// model
    endpoints are rejected unless ``ANILA_ALLOW_HTTP_ENDPOINT=1`` is set
    (PLAN.md P0.2, 2026-07-29: flag-gated uniformly, production included;
    default posture still rejects http).
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


def _build_response(
    model: ModelRegistry,
    *,
    caller: User | None = None,
    db: Session | None = None,
    is_service_token: bool = False,
) -> dict:
    """Serialize a model row.

    ``endpoint_url`` visibility uses the single predicate
    ``can_see_endpoint_address`` (owner, designated author, or service
    token). Everyone else receives ``ENDPOINT_INTERNAL`` /
    ``ENDPOINT_REDACTED`` based on ``is_internal``. The grouping-key
    field is retired (SYSTEM-MAP §6).
    """
    is_internal = bool(getattr(model, "is_internal", False))
    endpoint = visible_endpoint_url(
        model.endpoint_url,
        is_internal=is_internal,
        db=db,
        caller=caller,
        is_service_token=is_service_token,
    )
    data = {
        "id": model.id,
        "name": model.name,
        "display_name": model.display_name,
        "model_type": model.model_type,
        "endpoint_url": endpoint,
        "api_version": model.api_version,
        "is_active": model.is_active,
        "is_router_primary": bool(model.is_router_primary),
        "router_enabled": bool(getattr(model, "router_enabled", False)),
        "is_image_primary": bool(getattr(model, "is_image_primary", False)),
        "is_asr_primary": bool(getattr(model, "is_asr_primary", False)),
        "is_slides_primary": bool(getattr(model, "is_slides_primary", False)),
        "is_platform_embedding": bool(
            getattr(model, "is_platform_embedding", False)
        ),
        "embedding_native_dim": getattr(model, "embedding_native_dim", None),
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
        "thinking_effort": getattr(model, "thinking_effort", None),
        "temperature": getattr(model, "temperature", None),
        "top_p": getattr(model, "top_p", None),
        "presence_penalty": getattr(model, "presence_penalty", None),
        "max_tokens": getattr(model, "max_tokens", None),
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
    # Non-admin/owner: inference assignments OR (designated author +
    # rows they created). Authorship is independent of allowed_models so
    # an admin rewrite cannot erase the register → list → fix loop.
    # Owner inherits admin's full registry view (auth_service.require_admin
    # covers both, but this list query uses a direct role check so we
    # need to keep the tier explicit here too).
    if current_user.role not in ("admin", "owner"):
        clauses = []
        allowed_ids = [m.id for m in current_user.allowed_models]
        if allowed_ids:
            clauses.append(ModelRegistry.id.in_(allowed_ids))
        if can_set_endpoint_address(db, current_user):
            clauses.append(
                ModelRegistry.created_by_user_id == current_user.id
            )
        if not clauses:
            return []
        query = query.filter(or_(*clauses))
    return [
        _build_response(m, caller=current_user, db=db)
        for m in query.all()
    ]


async def _run_thinking_probe(
    *,
    probe_target,
    level: str | None,
    model_type: str | None,
    protocol: str | None,
) -> dict | None:
    """Ask the endpoint once whether it accepts ``level`` (r1_0039).

    Returns the ``thinking_probe`` block to attach to the response, or
    ``None`` when the combination is not worth probing (NONE / NULL sends no
    ``reasoning_effort`` at all; non-LLM and triton_grpc rows never carry
    one). An explicit upstream rejection becomes a 422 so the level cannot
    be written — a saved level the endpoint refuses turns every later chat
    call into a 400 that the console has no way to explain.

    Everything else is advisory: an unreachable endpoint must not stop an
    administrator from registering a model that is merely down right now.
    """
    if not level or level not in PROBEABLE_LEVELS:
        return None
    if model_type not in ("llm", "vlm") or (protocol or "openai_compatible") != "openai_compatible":
        return None
    result = await probe_thinking_effort(probe_target, level)
    if result.status == "rejected":
        raise HTTPException(
            status_code=422,
            detail=(
                f"模型端點不接受 thinking_effort={level}，未儲存。上游回覆："
                + (result.detail or "（未提供支援等級清單）")
            ),
        )
    return {"status": result.status, "detail": result.detail}


@router.post("", response_model=ModelResponse)
async def create_model(
    request: ModelCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # P4.6b: creating a row always sets an address — owner or designated
    # developer only. Undesignated administrators must use bulk import
    # from an already-registered row (no address entry).
    require_endpoint_address_author(db, current_user)

    existing = db.query(ModelRegistry).filter(ModelRegistry.name == request.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="模型名稱已存在")

    _enforce_endpoint_url(request.endpoint_url)

    protocol = request.protocol or "openai_compatible"
    if protocol not in ALLOWED_PROTOCOLS:
        raise HTTPException(
            status_code=422,
            detail=(
                "protocol 僅支援 openai_compatible 或 triton_grpc;"
                "custom_adapter 未被 proxy 實作,請勿選用"
            ),
        )
    _enforce_protocol_endpoint(protocol, request.endpoint_url)

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
    # Authorship for designated-author list/fetch; never grants inference.
    model.created_by_user_id = current_user.id
    if api_key:
        model.api_key_secret_ref = encode_service_token_envelope(api_key)

    # Probe before the row exists in the session: a rejected level raises
    # 422 here and nothing was added.
    thinking_probe = await _run_thinking_probe(
        probe_target=model,
        level=model.thinking_effort,
        model_type=model.model_type,
        protocol=protocol,
    )

    db.add(model)
    db.commit()
    db.refresh(model)
    log_audit_event(
        db,
        actor=current_user,
        action="create",
        resource_type="model",
        resource_id=model.id,
        detail=f"建立模型「{model.display_name}」",
        commit=True,
    )
    response = _build_response(model, caller=current_user, db=db)
    if thinking_probe is not None:
        response["thinking_probe"] = thinking_probe
    return response


def _upstream_models_url(endpoint_url: str) -> str:
    """Build OpenAI-compatible ``GET …/v1/models`` URL from a registered endpoint.

    Registry rows may store a bare host or a versioned base (``…/v1``). Both
    conventions join to the same final listing URL. If the stored value already
    ends with ``/models``, return it unchanged.
    """
    base = (endpoint_url or "").rstrip("/")
    if base.endswith("/models"):
        return base
    return join_upstream_path(endpoint_url or "", "/v1/models")


def _bulk_import_listing_name(raw_id: object) -> str | None:
    """Normalise an upstream listing id with the same length rule as the apply loop.

    Returns ``None`` when the id is blank or longer than 200 characters — those
    entries must not enter ``listed_names``, or a registry row could be wrongly
    reported as missing / retired.
    """
    if raw_id is None:
        return None
    name = str(raw_id).strip()
    if not name or len(name) > 200:
        return None
    return name


# Fixed client-facing text when import rejects a source endpoint via the
# outbound guard and the viewer may not see addresses. Viewers who can see
# receive the structured/plain guard detail (same as create/update).
_BULK_IMPORT_OUTBOUND_REJECTED = (
    "來源端點未通過外連檢查；請由擁有者或獲授權者檢視該列位址後再試"
)


def _endpoint_for_viewer(
    source: ModelRegistry,
    viewer: User,
    *,
    db: Session,
) -> str:
    """Response / audit-detail form of a source endpoint for ``viewer``."""
    return visible_endpoint_url(
        source.endpoint_url,
        is_internal=bool(getattr(source, "is_internal", False)),
        db=db,
        caller=viewer,
    )

def _parse_upstream_models_payload(payload: object) -> list:
    """Accept only recognised OpenAI-style shapes; anything else is a hard failure.

    Recognised:
      - ``{"data": [ ... ]}`` (OpenAI ``/v1/models``)
      - a bare JSON array

    A dict without ``data`` (including error bodies under HTTP 200) or a
    non-list ``data`` value raises 502 with the same failure class the
    single-record path uses for a bad upstream — never a silent empty success.
    """
    if isinstance(payload, dict):
        if "data" not in payload:
            raise HTTPException(
                status_code=502,
                detail="上游模型清單格式無法辨識",
            )
        data = payload["data"]
    elif isinstance(payload, list):
        data = payload
    else:
        raise HTTPException(
            status_code=502,
            detail="上游模型清單格式無法辨識",
        )
    if not isinstance(data, list):
        raise HTTPException(
            status_code=502,
            detail="上游模型清單 data 不是陣列",
        )
    return data


def _client_safe_upstream_error(
    exc: BaseException, *, http_status: int | None = None
) -> str:
    """Fixed client-facing message — never interpolate exception text (URLs)."""
    if http_status is not None:
        return f"無法從上游取得模型清單（HTTP {http_status}）"
    return f"無法從上游取得模型清單（{type(exc).__name__}）"


async def _fetch_upstream_model_listing(
    endpoint_url: str, api_key: str | None
) -> list:
    """GET upstream ``/models`` listing. Stub this in unit tests — no network.

    Returns the raw ``data`` array (may contain non-object entries). Callers
    must route non-objects into the skipped list rather than dropping them.
    Response body size is bounded by streaming (abort past the cap); failures
    return a fixed message (no URL).
    """
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    url = _upstream_models_url(endpoint_url)
    too_large_detail = (
        f"上游模型清單回應過大"
        f"（上限 {_BULK_IMPORT_BODY_MAX_BYTES} 位元組）"
    )
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    logger.exception(
                        "bulk import upstream HTTP error status=%s url=%s",
                        exc.response.status_code,
                        url,
                    )
                    raise HTTPException(
                        status_code=502,
                        detail=_client_safe_upstream_error(
                            exc, http_status=exc.response.status_code
                        ),
                    ) from exc

                # Cheap pre-filter when the gateway declares Content-Length.
                declared = resp.headers.get("content-length")
                if declared is not None:
                    try:
                        declared_n = int(declared)
                    except ValueError:
                        declared_n = None
                    else:
                        if declared_n > _BULK_IMPORT_BODY_MAX_BYTES:
                            logger.error(
                                "bulk import upstream body too large "
                                "content-length=%s cap=%s",
                                declared_n,
                                _BULK_IMPORT_BODY_MAX_BYTES,
                            )
                            raise HTTPException(
                                status_code=502,
                                detail=too_large_detail,
                            )

                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > _BULK_IMPORT_BODY_MAX_BYTES:
                        logger.error(
                            "bulk import upstream body too large "
                            "bytes=%s cap=%s",
                            total,
                            _BULK_IMPORT_BODY_MAX_BYTES,
                        )
                        raise HTTPException(
                            status_code=502,
                            detail=too_large_detail,
                        )
                    chunks.append(chunk)
                raw = b"".join(chunks)
                try:
                    payload = json.loads(raw)
                except ValueError as exc:
                    logger.exception(
                        "bulk import upstream JSON parse failed url=%s", url
                    )
                    raise HTTPException(
                        status_code=502,
                        detail=_client_safe_upstream_error(exc),
                    ) from exc
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        logger.exception("bulk import upstream transport error url=%s", url)
        raise HTTPException(
            status_code=502,
            detail=_client_safe_upstream_error(exc),
        ) from exc

    return _parse_upstream_models_payload(payload)


def _infer_model_type(model_id: str) -> str:
    lowered = model_id.lower()
    if "embed" in lowered:
        return "embedding"
    return "llm"


def _entry_description(entry: dict) -> str | None:
    """Best-effort description from an OpenAI-style listing entry (often absent).

    ``owned_by`` is upstream provenance, not administrator-authored text — mark
    it so later readers do not treat it as a human-written description.
    """
    desc = entry.get("description")
    if isinstance(desc, str) and desc.strip():
        return desc.strip()[:2000]
    owned = entry.get("owned_by")
    if isinstance(owned, str) and owned.strip():
        marked = f"[上游來源 owned_by] {owned.strip()}"
        return marked[:2000]
    return None


def _entry_optional_context_window(entry: dict) -> int | None:
    for key in ("context_window", "max_model_len", "max_tokens"):
        val = entry.get(key)
        if isinstance(val, int) and val > 0:
            return val
        if isinstance(val, str) and val.isdigit() and int(val) > 0:
            return int(val)
    return None


def _entry_optional_bool(entry: dict, key: str) -> bool | None:
    val = entry.get(key)
    return val if isinstance(val, bool) else None


def _inherited_create_kwargs(source: ModelRegistry) -> dict:
    """Build ModelCreate kwargs from ``_IMPORT_INHERITED_FROM_SOURCE`` only."""
    out: dict = {}
    for field in _IMPORT_INHERITED_FROM_SOURCE:
        val = getattr(source, field, None)
        if field == "api_version":
            out[field] = val or "v1"
        elif field == "protocol":
            out[field] = val or "openai_compatible"
        elif field == "is_internal":
            out[field] = bool(val) if val is not None else True
        elif field == "endpoint_url":
            out[field] = val
        else:
            out[field] = val
    return out


def _validate_source_ceiling_or_raise(source: ModelRegistry) -> None:
    """Fail once with an actionable message before the per-entry loop."""
    ceiling = getattr(source, "classification_ceiling", None)
    if ceiling is None:
        return
    try:
        ClassificationLevel.from_storage(ceiling)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=(
                f"來源模型的分類上限「{ceiling}」已不在現行字彙，"
                f"請先修正來源列後再整批帶入"
            ),
        ) from None


def _registry_by_name(db: Session) -> dict[str, ModelRegistry]:
    """Name → row snapshot. Extracted so tests can blind it (race simulation)."""
    return {row.name: row for row in db.query(ModelRegistry).all()}


def _unchanged_reason_for_existing(
    existing: ModelRegistry, source: ModelRegistry, *, filled: list[str] | None = None
) -> str:
    """Explain an already-present name; call out a different endpoint plainly."""
    other_endpoint = existing.endpoint_url != source.endpoint_url
    if filled:
        base = (
            "已登錄"
            + ("於其他端點" if other_endpoint else "")
            + "；已補齊空白欄位: "
            + ", ".join(filled)
            + "；其餘本機設定已保留"
        )
        return base
    if other_endpoint:
        return "已登錄於其他端點；本機設定已保留"
    return "已登錄；本機設定已保留"


def _apply_bulk_import_entries(
    *,
    db: Session,
    source: ModelRegistry,
    entries: list,
) -> tuple[
    list[ModelBulkImportCreated],
    list[ModelBulkImportUnchanged],
    list[ModelBulkImportSkipped],
    list[ModelBulkImportMissing],
    int,
]:
    """Idempotent apply of an upstream listing against the registry.

    Match key = ``ModelRegistry.name`` (same unique identity as single-record
    create). Existing rows keep local settings; only NULL/empty fillable
    fields may be populated from upstream metadata when the endpoint matches.
    Malformed entries are skipped with a reason — the batch continues.

    New rows inherit only endpoint-scoped facts from
    ``_IMPORT_INHERITED_FROM_SOURCE``. Per-model window/capability fields use
    schema defaults (or upstream values when present) and are listed in
    ``guessed_fields``. ``is_active=False`` pending administrator review.

    ``_BULK_IMPORT_ENTRY_CAP`` bounds *newly created* rows (not entries
    scanned). Already-present / skipped entries still get full accounting;
    a second run therefore progresses through the remainder of a long listing.
    """
    truncated = 0
    # Collect listing names with the same length rule as the create loop so
    # missing-from-listing does not false-report over-long ids.
    listed_names: set[str] = set()
    for entry in entries:
        if isinstance(entry, dict):
            normalised = _bulk_import_listing_name(entry.get("id"))
            if normalised is not None:
                listed_names.add(normalised)

    created_entries: list[ModelBulkImportCreated] = []
    unchanged: list[ModelBulkImportUnchanged] = []
    skipped: list[ModelBulkImportSkipped] = []

    existing_by_name = _registry_by_name(db)
    seen_in_batch: set[str] = set()

    for entry in entries:
        if not isinstance(entry, dict):
            skipped.append(
                ModelBulkImportSkipped(
                    name=None, reason="清單項目不是物件"
                )
            )
            continue

        raw_id = entry.get("id")
        if raw_id is None or not str(raw_id).strip():
            skipped.append(
                ModelBulkImportSkipped(name=None, reason="缺少或空白的模型 id")
            )
            continue
        name = str(raw_id).strip()
        if len(name) > 200:
            skipped.append(
                ModelBulkImportSkipped(
                    name=name[:200], reason="模型 id 超過 200 字元"
                )
            )
            continue
        if name in seen_in_batch:
            skipped.append(
                ModelBulkImportSkipped(
                    name=name, reason="上游清單重複的 id"
                )
            )
            continue
        seen_in_batch.add(name)

        existing = existing_by_name.get(name)
        if existing is not None:
            filled: list[str] = []
            # Only fill absent fields when the row belongs to this gateway.
            same_endpoint = existing.endpoint_url == source.endpoint_url
            if same_endpoint:
                upstream_desc = _entry_description(entry)
                if (
                    upstream_desc
                    and "description" in _IMPORT_FILLABLE_ABSENT
                    and not (existing.description or "").strip()
                ):
                    existing.description = upstream_desc
                    filled.append("description")
            unchanged.append(
                ModelBulkImportUnchanged(
                    name=name,
                    reason=_unchanged_reason_for_existing(
                        existing, source, filled=filled or None
                    ),
                )
            )
            continue

        # Cap newly created rows — keep scanning so already-present / skipped
        # still account, and a later run can create the remaining names.
        if len(created_entries) >= _BULK_IMPORT_ENTRY_CAP:
            truncated += 1
            continue

        inherited = _inherited_create_kwargs(source)
        guessed: list[str] = ["model_type", "display_name"]

        upstream_cw = _entry_optional_context_window(entry)
        if upstream_cw is not None:
            inherited["context_window"] = upstream_cw
        else:
            # Leave empty (ModelCreate default None) — not sibling-model sized.
            guessed.append("context_window")

        for flag, default in (
            ("supports_streaming", True),
            ("supports_json_schema", False),
            ("supports_tools", False),
        ):
            upstream_flag = _entry_optional_bool(entry, flag)
            if upstream_flag is not None:
                inherited[flag] = upstream_flag
            else:
                inherited[flag] = default
                guessed.append(flag)

        try:
            create_payload = ModelCreate(
                name=name,
                display_name=name,
                model_type=_infer_model_type(name),
                description=_entry_description(entry),
                **inherited,
            )
        except ValidationError as exc:
            skipped.append(
                ModelBulkImportSkipped(
                    name=name,
                    reason=f"驗證失敗: {exc.errors()[0]['msg']}",
                )
            )
            continue

        data = create_payload.model_dump()
        data.pop("api_key", None)
        model = ModelRegistry(**data)
        # Inferred model_type / display_name are not faithful gateway facts —
        # leave inactive until an administrator reviews and activates.
        model.is_active = False
        # Reuse the source endpoint's stored credential envelope (same gateway);
        # never invent a new key. Plaintext is never written here.
        if getattr(source, _IMPORT_COPY_SECRET_REF, None):
            setattr(
                model,
                _IMPORT_COPY_SECRET_REF,
                getattr(source, _IMPORT_COPY_SECRET_REF),
            )

        try:
            with db.begin_nested():
                db.add(model)
                db.flush()
        except IntegrityError:
            # Concurrent registration of the same name after our snapshot —
            # report that name rather than aborting the whole batch.
            raced = (
                db.query(ModelRegistry)
                .filter(ModelRegistry.name == name)
                .first()
            )
            if raced is None:
                skipped.append(
                    ModelBulkImportSkipped(
                        name=name, reason="寫入衝突，請重試"
                    )
                )
                continue
            existing_by_name[name] = raced
            unchanged.append(
                ModelBulkImportUnchanged(
                    name=name,
                    reason=_unchanged_reason_for_existing(raced, source),
                )
            )
            continue

        existing_by_name[name] = model
        created_entries.append(
            ModelBulkImportCreated(name=name, guessed_fields=guessed)
        )

    # Report-only: registry rows on this endpoint absent from the listing.
    missing: list[ModelBulkImportMissing] = []
    for row in existing_by_name.values():
        if row.endpoint_url != source.endpoint_url:
            continue
        if row.name in listed_names:
            continue
        missing.append(ModelBulkImportMissing(name=row.name))

    return created_entries, unchanged, skipped, missing, truncated


def _audit_name_sample(names: list[str]) -> list[str]:
    return names[:_BULK_IMPORT_AUDIT_NAME_SAMPLE]


@router.post("/import", response_model=ModelBulkImportResponse)
async def import_models_from_endpoint(
    body: ModelBulkImportRequest,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """P4.6 / SYSTEM-MAP §6: pull upstream ``/v1/models`` into the registry.

    Auth = same ``require_admin`` gate as ``POST /api/models``. SSRF / scheme
    rules reuse ``_enforce_endpoint_url`` (identical to single-record create).
    Credential resolution reuses ``resolve_model_gateway_key`` (per-model ref,
    then global ``MODEL_GATEWAY_API_KEY``).
    """
    source = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.id == body.source_model_id)
        .first()
    )
    if not source:
        raise HTTPException(status_code=404, detail="來源模型不存在")

    # Bulk import is an OpenAI ``GET …/v1/models`` listing walk, and it copies
    # the source row's endpoint_url + protocol onto every created row without
    # re-running _enforce_protocol_endpoint. From a triton_grpc source that
    # built ``grpc://host:9001/v1/models``, which passes the SSRF guard (the
    # scheme is legal for model endpoints) and then dies inside httpx as a
    # generic 502 — an unactionable error for something that can never work,
    # since gRPC has no listing endpoint. Refuse up front and say why.
    if (getattr(source, "protocol", None) or "") == "triton_grpc":
        raise HTTPException(
            status_code=422,
            detail=(
                "protocol=triton_grpc 的來源不支援整批帶入："
                "gRPC 沒有 OpenAI 相容的 /v1/models 列表可以掃描。"
                "請逐一註冊 Triton 模型（名稱須與 Triton 上的 model name 一致）"
            ),
        )

    # Same outbound guard as create/update — disallowed endpoints stay
    # disallowed. Guard the FINAL listing URL (never guard one string and
    # request another). Import path must not echo hostname / resolved address
    # to a viewer who may not see the redacted row's URL; designated viewers
    # and the owner receive the same structured/plain guard detail as create.
    listing_url = _upstream_models_url(source.endpoint_url)
    try:
        _enforce_endpoint_url(listing_url)
    except HTTPException as exc:
        if exc.status_code == 400:
            logger.warning(
                "bulk import outbound guard rejected source_model_id=%s detail=%s",
                source.id,
                exc.detail,
            )
            if can_see_endpoint_address(db, admin):
                raise
            raise HTTPException(
                status_code=400,
                detail=_BULK_IMPORT_OUTBOUND_REJECTED,
            ) from exc
        raise
    # Fail closed once if the source ceiling is outside the current vocabulary.
    _validate_source_ceiling_or_raise(source)

    api_key = resolve_model_gateway_key(source)
    listing_endpoint = source.endpoint_url
    # Release pooled connection before the outbound listing fetch (30s).
    db.commit()
    entries = await _fetch_upstream_model_listing(listing_endpoint, api_key)

    # Database loop off the event loop (batch-approve-style bound sync work).
    created_entries, unchanged, skipped, missing, truncated = await asyncio.to_thread(
        _apply_bulk_import_entries,
        db=db,
        source=source,
        entries=entries,
    )
    db.commit()

    created_names = [e.name for e in created_entries]
    # Response redacts via the single visibility predicate. Audit ``detail``
    # ALWAYS stores a sentinel — listing returns detail to every admin —
    # while the real address lives in metadata; ``serialize_audit_log``
    # substitutes it back for viewers who may see addresses.
    response_endpoint = _endpoint_for_viewer(source, admin, db=db)
    detail_sentinel = (
        ENDPOINT_INTERNAL
        if getattr(source, "is_internal", False)
        else ENDPOINT_REDACTED
    )
    log_audit_event(
        db,
        actor=admin,
        action="bulk_import",
        resource_type="model",
        resource_id=source.id,
        detail=(
            f"整批帶入模型自 endpoint「{detail_sentinel}」"
            f"（source_model_id={source.id}）:"
            f" created={len(created_names)}"
            f" already_existed={len(unchanged)}"
            f" skipped={len(skipped)}"
            f" truncated={truncated}"
            f" missing_from_listing={len(missing)}"
        ),
        ip_address=_client_ip(request),
        metadata={
            "source_model_id": source.id,
            "endpoint_url": source.endpoint_url,
            "created": len(created_names),
            "already_existed": len(unchanged),
            "skipped": len(skipped),
            "truncated": truncated,
            "missing_from_listing": len(missing),
            "created_names_sample": _audit_name_sample(created_names),
            "created_names_total": len(created_names),
        },
        commit=True,
    )
    return ModelBulkImportResponse(
        source_model_id=source.id,
        endpoint_url=response_endpoint,
        created=len(created_names),
        already_existed=len(unchanged),
        skipped=len(skipped),
        truncated=truncated,
        created_names=created_names,
        created_entries=created_entries,
        unchanged=unchanged,
        skipped_entries=skipped,
        missing_from_listing=missing,
    )


@router.post(
    "/import/activate-created",
    response_model=ModelBulkActivateCreatedResponse,
)
def activate_created_from_import(
    body: ModelBulkActivateCreatedRequest,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Activate inactive rows produced by a prior bulk import (one review step).

    Keeps the inactive-by-default fence: nothing becomes routable until this
    (or per-row activate) runs. Scoped to ``names`` that still share the
    source endpoint.
    """
    if len(body.names) > _BULK_ACTIVATE_INPUT_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"單次最多啟用 {_BULK_ACTIVATE_INPUT_LIMIT} 個模型",
        )

    source = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.id == body.source_model_id)
        .first()
    )
    if not source:
        raise HTTPException(status_code=404, detail="來源模型不存在")

    activated_names: list[str] = []
    already_active = 0
    not_found = 0
    wrong_endpoint = 0
    # Preserve caller order; de-dupe while counting.
    seen: set[str] = set()
    for raw in body.names:
        name = (raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        row = db.query(ModelRegistry).filter(ModelRegistry.name == name).first()
        if row is None:
            not_found += 1
            continue
        if row.endpoint_url != source.endpoint_url:
            wrong_endpoint += 1
            continue
        if row.is_active:
            already_active += 1
            continue
        row.is_active = True
        activated_names.append(name)

    db.commit()
    log_audit_event(
        db,
        actor=admin,
        action="bulk_activate_created",
        resource_type="model",
        resource_id=source.id,
        detail=(
            f"整批啟用帶入模型（source_model_id={source.id}）:"
            f" activated={len(activated_names)}"
            f" already_active={already_active}"
            f" not_found={not_found}"
            f" wrong_endpoint={wrong_endpoint}"
        ),
        ip_address=_client_ip(request),
        metadata={
            "source_model_id": source.id,
            "activated": len(activated_names),
            "activated_names_sample": _audit_name_sample(activated_names),
            "activated_names_total": len(activated_names),
        },
        commit=True,
    )
    return ModelBulkActivateCreatedResponse(
        source_model_id=source.id,
        activated=len(activated_names),
        already_active=already_active,
        not_found=not_found,
        wrong_endpoint=wrong_endpoint,
        activated_names=activated_names,
    )


@router.get("/router-primary")
def get_router_primary(
    identity: agent_credential_service.CallerIdentity | None = Depends(
        verify_service_token
    ),
    db: Session = Depends(get_db),
):
    """Return the LLM designated as ANILA Router's primary model.

    Service-to-service endpoint consumed by anila-core-router at boot and on
    TTL refresh. Returns 404 when no primary is set so the caller can fall
    back to a clear "no model available" error instead of silently using the
    wrong upstream.

    Admitted principals:
      * ``service_client`` with ``client_type='router'``.
        Live: the host fleet secret is seeded as ``service_clients``
        ``client_name='router-primary'`` (``is_legacy=TRUE``; numeric
        ``id`` is whatever the DB allocated — do not assume ``1``);
        csp/router/studio/asr-gateway all present that same secret, so
        they share this ``router`` attribution — not "only the router
        process".
    Rejected: ``agent``-kind tokens; other ``client_type`` values;
    unattributed legacy env (``identity is None``) — a ``client_type``
    restriction fails closed so deactivating the owning row (or rotating
    ``CSP_SERVICE_TOKEN`` in ``.env`` without also rotating that DB row)
    cannot silently fall through to an unconditional allow.
    """
    # KIND GATE — remove this call to prove A1 red for router-primary.
    require_admitted_service_principal(
        identity,
        db=db,
        allowed_kinds=("service_client",),
        allowed_client_types=("router",),
        # Must be False: unattributed legacy cannot prove client_type
        # (helper also refuse-closed if these ever disagree).
        # LANDMINE: rotating CSP_SERVICE_TOKEN in .env without rotating
        # the router-primary DB row → identity is None → 403 here.
        allow_legacy_env=False,
        endpoint="GET /api/models/router-primary",
    )
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
        "thinking_effort": getattr(model, "thinking_effort", None),
        "temperature": getattr(model, "temperature", None),
        "top_p": getattr(model, "top_p", None),
        "presence_penalty": getattr(model, "presence_penalty", None),
        "max_tokens": getattr(model, "max_tokens", None),
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
    from app.services.router_model_policy import RouterModelPolicyError, require_campus_default_eligible
    try:
        require_campus_default_eligible(db, model)
    except RouterModelPolicyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    # Clear previous primary only after eligibility is proven, same transaction.
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
    return _build_response(model, caller=admin, db=db)


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
        return _build_response(model, caller=admin, db=db)

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
    return _build_response(model, caller=admin, db=db)


# ── 主簡報模型（slides-primary）— 2026-09-02，比照 image-primary 三件組 ──────


@router.get("/slides-primary")
def get_slides_primary(
    request: Request,
    db: Session = Depends(get_db),
    x_csp_service_token: str | None = Header(default=None, alias="X-CSP-Service-Token"),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Return the LLM designated for slide-deck generation (anila-studio).

    anila-studio polls this with its service token (60 s TTL) so an admin
    can switch the deck model from the Models page without a restart. Same
    admission rules and endpoint-address visibility as ``/image-primary``;
    never returns a key.
    """
    is_svc = False
    caller: User | None = None
    if x_csp_service_token:
        identity = verify_service_token(request, db, x_csp_service_token)
        require_admitted_service_principal(
            identity,
            db=db,
            allowed_kinds=("service_client",),
            allowed_client_types=None,
            allow_legacy_env=True,
            endpoint="GET /api/models/slides-primary",
        )
        is_svc = True
    else:
        caller = get_current_user(request, credentials, db)
    model = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.is_slides_primary.is_(True))
        .first()
    )
    if not model:
        raise HTTPException(status_code=404, detail="尚未指定主簡報模型")
    if not model.is_active:
        raise HTTPException(status_code=409, detail="已指定的主簡報模型已被停用")
    return {
        "id": model.id,
        "name": model.name,
        "display_name": model.display_name,
        "model_type": model.model_type,
        "endpoint_url": visible_endpoint_url(
            model.endpoint_url,
            is_internal=bool(getattr(model, "is_internal", False)),
            db=db,
            caller=caller,
            is_service_token=is_svc,
        ),
        "api_version": model.api_version,
        "health_status": model.health_status,
    }


@router.post("/{model_id}/set-slides-primary", response_model=ModelResponse)
def set_slides_primary(
    model_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Mark an LLM as the slide-deck (Studio) model, clearing any previous one."""
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    if model.model_type != "llm":
        raise HTTPException(status_code=400, detail="僅 llm 類型可設為主簡報模型")
    if not model.is_active:
        raise HTTPException(status_code=400, detail="已停用的模型不能設為主簡報模型")
    (
        db.query(ModelRegistry)
        .filter(ModelRegistry.is_slides_primary.is_(True), ModelRegistry.id != model_id)
        .update({"is_slides_primary": False}, synchronize_session=False)
    )
    model.is_slides_primary = True
    db.commit()
    db.refresh(model)
    log_audit_event(
        db,
        actor=admin,
        action="set_slides_primary",
        resource_type="model",
        resource_id=model.id,
        detail=f"設為主簡報模型: {model.display_name}",
        commit=True,
    )
    return _build_response(model, caller=admin, db=db)


@router.post("/{model_id}/unset-slides-primary", response_model=ModelResponse)
def unset_slides_primary(
    model_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Remove the slide-deck model designation (idempotent)."""
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    if not model.is_slides_primary:
        return _build_response(model, caller=admin, db=db)
    model.is_slides_primary = False
    db.commit()
    db.refresh(model)
    log_audit_event(
        db,
        actor=admin,
        action="unset_slides_primary",
        resource_type="model",
        resource_id=model.id,
        detail=f"取消主簡報模型: {model.display_name}",
        commit=True,
    )
    return _build_response(model, caller=admin, db=db)


# ── Slice 8b image-primary designation ───────────────────────────────────────


@router.get("/image-primary")
def get_image_primary(
    request: Request,
    db: Session = Depends(get_db),
    x_csp_service_token: str | None = Header(default=None, alias="X-CSP-Service-Token"),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Return the model designated as the primary image (FLUX) model.

    Service-to-service consumers (flux2-dev-agent / anila-studio) send
    ``X-CSP-Service-Token``. Authenticated users may also read it; the
    address is shaped by the single visibility predicate
    ``can_see_endpoint_address`` / ``visible_endpoint_url`` — service
    tokens and designated callers see the real URL, everyone else gets
    the redaction sentinel. Never returns the model's API key.

    Admitted service-token principals:
      * ``service_client`` of any ``client_type`` (taxonomy:
        router|worker|admin_tool). Live studio traffic presents the
        fleet secret, which resolves as ``router-primary``
        (``client_type='router'``) via the DB row — not as unattributed
        legacy env.
      * unattributed legacy env (``identity is None``) — only reached
        when no active DB credential matches; kept for cutover / tests.
    Rejected on the service-token path: ``agent``-kind ``csk-``.
    """
    is_svc = False
    caller: User | None = None
    if x_csp_service_token:
        identity = verify_service_token(request, db, x_csp_service_token)
        # KIND GATE — remove this call to prove A1 red for image-primary.
        require_admitted_service_principal(
            identity,
            db=db,
            allowed_kinds=("service_client",),
            allowed_client_types=None,
            allow_legacy_env=True,
            endpoint="GET /api/models/image-primary",
        )
        is_svc = True
    else:
        caller = get_current_user(request, credentials, db)

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
        "endpoint_url": visible_endpoint_url(
            model.endpoint_url,
            is_internal=bool(getattr(model, "is_internal", False)),
            db=db,
            caller=caller,
            is_service_token=is_svc,
        ),
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
    return _build_response(model, caller=admin, db=db)


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
        return _build_response(model, caller=admin, db=db)

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
    return _build_response(model, caller=admin, db=db)


# ── ASR decoder primary designation ───────────────────────────────────────────


def _asr_row_own_key(model) -> str | None:
    """解出**該筆自己**掛的 ``api_key_secret_ref``;解不開就回 ``None``。

    ⚠ **刻意不用 ``resolve_model_gateway_key``。** 那一支(LLM proxy 在用的)
    在解密失敗時會**退回全域 ``MODEL_GATEWAY_API_KEY``**。對 LLM 出向那是
    合理的 fail-soft —— 同一個 gateway、本來就是同一把金鑰;對 ASR 卻是把
    「另一個服務的祕密」交到算力中心的辨識端點手上,跨了信任邊界。

    而且觸發條件很現實:輪替 ``SECRET_KEY``、或把資料庫還原進另一組
    金鑰的環境,**每一筆 ref 會同時變成解不開**。症狀是每一句話 401 ——
    跟「金鑰設錯」完全分不出來,正是這條路徑存在的理由所要避免的誤診。

    不變式:ASR payload 只帶「這一筆自己的金鑰」,否則什麼都不帶。
    """
    ref = getattr(model, "api_key_secret_ref", None)
    if not ref:
        return None
    try:
        key = decode_service_token_envelope(ref)
    except Exception:
        logger.warning(
            "asr-primary 的 api_key_secret_ref 解不開 model_id=%s;"
            "**不**退回全域 MODEL_GATEWAY_API_KEY,payload 不帶金鑰",
            getattr(model, "id", None),
            exc_info=True,
        )
        return None
    return (key or "").strip() or None


@router.get("/asr-primary")
def get_asr_primary(
    request: Request,
    db: Session = Depends(get_db),
    x_csp_service_token: str | None = Header(default=None, alias="X-CSP-Service-Token"),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Return the model designated as the primary ASR decoder.

    Service-to-service consumers (asr-gateway) send ``X-CSP-Service-Token``.
    Authenticated users may also read it; the address is shaped by
    ``visible_endpoint_url`` — service tokens and designated callers see the
    real URL, everyone else gets the redaction sentinel.

    ``api_key`` (2026-08-05):**只在服務權杖通道**上、而且**只有該筆自己掛了
    ``api_key_secret_ref``** 時才回傳。這是遠端解碼端唯一能拿到憑證的地方 ——
    算力中心的 gateway 要 ``Authorization: Bearer``,而 ASR 路徑原本連一個放
    金鑰的欄位都沒有,結果是每一句話 401。人類呼叫者永遠拿不到這個欄位。

    ⚠ **刻意不吃 ``MODEL_GATEWAY_API_KEY`` 全域退路** —— 不論是「這筆沒掛
    金鑰」還是「掛了但解不開」。兩種情形都回不帶 ``api_key`` 的 payload,
    讓 gateway 用自己的環境變數祕密;回傳全域模型金鑰會把一把不相干的祕密
    送去解碼端,而且症狀是 401 —— 跟「金鑰設錯」完全分不出來。解密那一半
    的理由寫在 ``_asr_row_own_key``。

    Admitted service-token principals:
      * ``service_client`` of any ``client_type``. Live asr-gateway
        presents the fleet secret, which resolves as ``router-primary``
        (``client_type='router'``) via the DB row — not as unattributed
        legacy env.
      * unattributed legacy env (``identity is None``) — only reached
        when no active DB credential matches; kept for cutover / tests.
    Rejected on the service-token path: ``agent``-kind ``csk-``.
    """
    is_svc = False
    caller: User | None = None
    if x_csp_service_token:
        identity = verify_service_token(request, db, x_csp_service_token)
        # KIND GATE — remove this call to prove A1 red for asr-primary.
        require_admitted_service_principal(
            identity,
            db=db,
            allowed_kinds=("service_client",),
            allowed_client_types=None,
            allow_legacy_env=True,
            endpoint="GET /api/models/asr-primary",
        )
        is_svc = True
    else:
        caller = get_current_user(request, credentials, db)

    model = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.is_asr_primary.is_(True))
        .first()
    )
    if not model:
        raise HTTPException(status_code=404, detail="尚未指定主語音辨識模型")
    if not model.is_active:
        raise HTTPException(status_code=409, detail="已指定的主語音辨識模型已被停用")
    payload = {
        "id": model.id,
        "name": model.name,
        "display_name": model.display_name,
        "model_type": model.model_type,
        "endpoint_url": visible_endpoint_url(
            model.endpoint_url,
            is_internal=bool(getattr(model, "is_internal", False)),
            db=db,
            caller=caller,
            is_service_token=is_svc,
        ),
        "api_version": model.api_version,
        "health_status": model.health_status,
    }
    # 祕密只走服務權杖通道,而且只有該筆真的掛了、而且解得開金鑰時才帶 ——
    # 沒掛(本地 asr-decoder)或解不開的那筆,payload 與 2026-08-05 之前
    # **逐欄位相同**。
    if is_svc:
        own_key = _asr_row_own_key(model)
        if own_key:
            payload["api_key"] = own_key
    return payload


@router.post("/{model_id}/set-asr-primary", response_model=ModelResponse)
def set_asr_primary(
    model_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Mark a model as the primary ASR decoder (clearing any previous one)."""
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    if model.model_type != "asr":
        raise HTTPException(status_code=400, detail="僅 asr 類型可設為主語音辨識模型")
    if not model.is_active:
        raise HTTPException(status_code=400, detail="已停用的模型不能設為主語音辨識模型")

    (
        db.query(ModelRegistry)
        .filter(ModelRegistry.is_asr_primary.is_(True), ModelRegistry.id != model_id)
        .update({"is_asr_primary": False}, synchronize_session=False)
    )
    model.is_asr_primary = True
    db.commit()
    db.refresh(model)
    log_audit_event(
        db,
        actor=admin,
        action="set_asr_primary",
        resource_type="model",
        resource_id=model.id,
        detail=f"設為主語音辨識模型: {model.display_name}",
        commit=True,
    )
    return _build_response(model, caller=admin, db=db)


@router.post("/{model_id}/unset-asr-primary", response_model=ModelResponse)
def unset_asr_primary(
    model_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Remove the primary ASR-decoder designation from a model."""
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    if not model.is_asr_primary:
        return _build_response(model, caller=admin, db=db)

    model.is_asr_primary = False
    db.commit()
    db.refresh(model)
    log_audit_event(
        db,
        actor=admin,
        action="unset_asr_primary",
        resource_type="model",
        resource_id=model.id,
        detail=f"取消主語音辨識模型: {model.display_name}",
        commit=True,
    )
    return _build_response(model, caller=admin, db=db)


# ── P4.8 platform embedding designation ──────────────────────────────────────


async def _probe_embedding_native_dim(
    model: ModelRegistry, tuning: ProxyTuning
) -> int:
    """Embed once through ``proxy_request`` and return len(vector).

    Uses the same path as live traffic (including ``api_version`` URL prefix
    for openai_compatible, and Triton gRPC for ``triton_grpc``) so the probe
    cannot disagree with the call path about the same model. Measured fact —
    never trust a configured value. Caller owns the session lifecycle.
    """
    from anila_core.memory.long_term import EMBED_DIM
    from app.services.proxy.service import proxy_request

    api_version = getattr(model, "api_version", None)
    if api_version not in ("v1", "v2"):
        api_version = "v1"
    try:
        response = await proxy_request(
            model=model,
            api_key_id=None,
            user_id=0,
            department_id=None,
            request_body={"model": model.name, "input": ["anila-dim-probe"]},
            endpoint_path=f"/{api_version}/embeddings",
            embedding_input_role="query",
            record_usage=False,
            tuning=tuning,
        )
        vec = response["data"][0]["embedding"]
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"探測 embedding 維度失敗: {type(exc).__name__}",
        ) from exc
    if not isinstance(vec, list) or not vec:
        raise HTTPException(
            status_code=502,
            detail="embedding 探測回傳空向量",
        )
    n = len(vec)
    if n < 1:
        raise HTTPException(status_code=502, detail="embedding 探測維度無效")
    # Soft ceiling: refuse absurd widths that can't fit the column even
    # with truncation (keeps a malicious/broken endpoint from writing
    # nonsense into embedding_native_dim). EMBED_DIM*4 is generous.
    if n > EMBED_DIM * 4:
        raise HTTPException(
            status_code=502,
            detail=f"embedding 探測維度 {n} 超出合理範圍",
        )
    return n


@router.get("/platform-embedding")
def get_platform_embedding(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the designated platform embedding model + pending-recompute counts.

    Auth: any authenticated user may read (collections need the default;
    memory needs the name). Setting remains admin+.
    """
    from anila_core.memory.long_term import EMBED_DIM
    from app.services.platform_embedding import (
        count_pending_recompute,
        resolve_platform_embedding,
    )

    resolved = resolve_platform_embedding(db)
    pending = count_pending_recompute(
        db, resolved.name if resolved else None
    )
    if resolved is None:
        return {
            "designated": None,
            "storage_dim": EMBED_DIM,
            "pending_recompute": pending,
        }
    return {
        "designated": {
            "id": resolved.model.id,
            "name": resolved.name,
            "display_name": resolved.model.display_name,
            "native_dim": resolved.native_dim,
            "truncates": resolved.truncates,
            "is_designated": bool(resolved.model.is_platform_embedding),
        },
        "storage_dim": EMBED_DIM,
        "pending_recompute": pending,
    }


_STRANDED_NAMES_SHOWN = 5


def _collections_not_indexed_under(db: Session, designated_name: str) -> list[str]:
    """Active collections whose vectors a given embedding model cannot retrieve.

    Reads ``ingestion_collections.embedding_model`` — the platform's own
    per-collection record of which model produced that collection's
    vectors. Retrieval filters chunks on exactly that provenance, so any
    collection whose recorded model differs from ``designated_name`` is a
    collection that will answer nothing once ``designated_name`` is the
    designation.

    This is a question about the **state of the corpus**, not about the
    shape of the transition, and that distinction is the whole point.
    The previous version compared the incoming model against whatever
    ``resolve_platform_embedding`` returned beforehand, which silently
    lost the warning for an ordinary operator sequence: deactivating the
    old model first clears ``is_platform_embedding`` (``deactivate_model``
    below), so the "previous" name resolved to the soft fallback — often
    the very model being designated — and the comparison came out equal
    while the corpus was every bit as stranded. Asking the collections
    gives the same answer whichever order the operator worked in.

    ``ingestion_collections`` carries no row-level security (unlike
    ``document_chunks``), so an unscoped admin session sees every row and
    the count is honest. One indexed query; cost tracks the number of
    collections, not the number of chunks.
    """
    from sqlalchemy import func

    from app.models.ingestion import IngestionCollection

    # Case-insensitive on purpose. This column DEFAULTs to
    # ``nvidia/NV-embed-V2`` while the same model registers as
    # ``nvidia/nv-embed-v2``, so a case-sensitive ``!=`` reports a
    # default-valued collection as stranded against its own model. A
    # warning that is wrong on the platform's most common configuration
    # teaches the operator to ignore the warning.
    rows = (
        db.query(IngestionCollection.name)
        .filter(
            IngestionCollection.status == "active",
            func.lower(IngestionCollection.embedding_model)
            != func.lower(designated_name),
        )
        .order_by(IngestionCollection.id)
        .all()
    )
    return [r.name for r in rows]


@router.post("/{model_id}/set-platform-embedding")
async def set_platform_embedding(
    model_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Designate an embedding model as the platform default.

    Probes the live endpoint once to measure native dimension. When
    native > 4000 (pgvector halfvec HNSW ceiling) the response carries
    ``truncation_warning`` so the UI can say so plainly.

    When any active collection is indexed under a different model, the
    response also carries ``index_mismatch_warning`` and the offending
    ``stranded_collections``. Retrieval filters chunks on the model that
    produced them, so from the moment this call returns, everything
    indexed under another model is unreachable. The operator is the only
    person who can act on that, and this call is the moment they can
    still act cheaply — telling them here beats letting a user discover
    it as a knowledge base that answers nothing.

    The check reads the corpus (``_collections_not_indexed_under``), not
    the before/after pair, so it does not depend on the operator having
    designated straight over the old model rather than deactivating it
    first.
    """
    from anila_core.memory.long_term import EMBED_DIM

    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    if model.model_type != "embedding":
        raise HTTPException(
            status_code=400, detail="僅 embedding 類型可設為平台主 embedding"
        )
    if not model.is_active:
        raise HTTPException(
            status_code=400, detail="已停用的模型不能設為平台主 embedding"
        )

    # What embed callers resolve to *right now* — the designated row, or
    # the soft fallback when nobody has designated one yet. Either way it
    # is the name existing vectors were written under, so it is the name
    # this change strands. Captured as a plain str before any commit:
    # the ORM instance behind it expires and we must not re-load it after
    # the designation flips.
    from app.services.platform_embedding import resolve_platform_embedding

    _previous = resolve_platform_embedding(db)
    previous_name: str | None = _previous.name if _previous is not None else None

    # Snapshot fields the probe needs, then release the pooled connection
    # before the outbound HTTP call (same posture as _embed_query).
    from types import SimpleNamespace

    probe_snap = SimpleNamespace(
        id=model.id,
        name=model.name,
        display_name=model.display_name,
        model_type=model.model_type,
        endpoint_url=model.endpoint_url,
        api_version=model.api_version,
        protocol=model.protocol or "openai_compatible",
        api_key_secret_ref=model.api_key_secret_ref,
        is_active=model.is_active,
    )
    # 逾時／重試在 commit 之前解（commit 把池化連線還回去，之後不該再查 DB）。
    probe_tuning = resolve_proxy_tuning(db)
    db.commit()
    native_dim = await _probe_embedding_native_dim(probe_snap, probe_tuning)

    (
        db.query(ModelRegistry)
        .filter(
            ModelRegistry.is_platform_embedding.is_(True),
            ModelRegistry.id != model_id,
        )
        .update({"is_platform_embedding": False}, synchronize_session=False)
    )
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if model is None:
        raise HTTPException(status_code=404, detail="模型不存在")
    model.is_platform_embedding = True
    model.embedding_native_dim = native_dim
    db.commit()
    db.refresh(model)

    truncates = native_dim > EMBED_DIM
    truncation_warning = None
    if truncates:
        truncation_warning = (
            f"此模型原生維度為 {native_dim}，超過 pgvector halfvec HNSW 上限 "
            f"{EMBED_DIM}，寫入時會截斷尾端 {native_dim - EMBED_DIM} 維。"
            "這是資料庫限制，不是設定錯誤。"
        )

    stranded = _collections_not_indexed_under(db, model.name)
    has_stranded_collections = bool(stranded)
    index_mismatch_warning = None
    if stranded:
        shown = "、".join(stranded[:_STRANDED_NAMES_SHOWN])
        if len(stranded) > _STRANDED_NAMES_SHOWN:
            shown += f"⋯（共 {len(stranded)} 個）"
        revert_hint = (
            f"把平台主 embedding 改回「{previous_name}」即可立刻恢復，不會動到資料。"
            if previous_name and previous_name != model.name
            else "把平台主 embedding 改回這些知識庫原本使用的模型即可立刻恢復。"
        )
        index_mismatch_warning = (
            f"平台主 embedding 已設為「{model.name}」，"
            f"但下列知識庫的索引是以其他模型建立的：{shown}。"
            "檢索只會取用以現行模型建立索引的段落，"
            "所以這些知識庫現在檢索不到任何內容——"
            "它們在搜尋時會明確回報索引模型不一致，不會靜靜地回空結果。"
            f"{revert_hint}"
            "若確定要換模型，平台目前沒有重新索引的功能，"
            "必須把這些知識庫裡的文件逐份刪除後重新上傳。"
        )

    log_audit_event(
        db,
        actor=admin,
        action="set_platform_embedding",
        resource_type="model",
        resource_id=model.id,
        detail=(
            f"設為平台主 embedding: {model.display_name} "
            f"(native_dim={native_dim}"
            f"{', truncates' if truncates else ''}"
            f"{f', 取代 {previous_name}' if previous_name and previous_name != model.name else ''}"
            f"{f', {len(stranded)} 個知識庫檢索不到' if stranded else ''})"
        ),
        metadata={
            "native_dim": native_dim,
            "storage_dim": EMBED_DIM,
            "truncates": truncates,
            "previous_embedding_model": previous_name,
            "has_stranded_collections": has_stranded_collections,
            "stranded_collections": stranded,
        },
        commit=True,
    )
    body = _build_response(model, caller=admin, db=db)
    body["truncation_warning"] = truncation_warning
    body["measured_native_dim"] = native_dim
    # Only ``index_mismatch_warning`` goes back on the wire: the console
    # renders it, and it carries the collection names and the remedy in
    # its own text. ``stranded_collections`` / ``previous_embedding_model``
    # were returned here and read by nobody — the same send-and-ignore
    # shape this package exists to remove. The structured form lives in
    # the audit metadata above, which is durable and queryable.
    body["index_mismatch_warning"] = index_mismatch_warning
    return body


@router.post("/{model_id}/unset-platform-embedding", response_model=ModelResponse)
def unset_platform_embedding(
    model_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Remove the platform embedding designation from a model."""
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    if not getattr(model, "is_platform_embedding", False):
        return _build_response(model, caller=admin, db=db)

    model.is_platform_embedding = False
    db.commit()
    db.refresh(model)
    log_audit_event(
        db,
        actor=admin,
        action="unset_platform_embedding",
        resource_type="model",
        resource_id=model.id,
        detail=f"取消平台主 embedding: {model.display_name}",
        commit=True,
    )
    return _build_response(model, caller=admin, db=db)


@router.get("/{model_id}", response_model=ModelResponse)
def get_model(
    model_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    if not _caller_may_view_model(db, current_user, model):
        raise HTTPException(status_code=404, detail="模型不存在")
    return _build_response(model, caller=current_user, db=db)


@router.put("/{model_id}", response_model=ModelResponse)
async def update_model(
    model_id: int,
    request: ModelUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Admin-tier keeps full updates. Designated developers may update
    # address only on rows they authored (same basis as read). Plain
    # users stay out.
    if not (
        is_admin_tier(current_user)
        or can_set_endpoint_address(db, current_user)
    ):
        raise HTTPException(status_code=403, detail="需要管理員權限")

    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")

    # Below admin-tier: write basis == read authorship basis. When the row is
    # invisible to the caller the refusal must match get_model, so a successful
    # address write cannot confirm that a foreign id exists. When the caller can
    # already see the row (assigned to them, but not theirs to author), 404 would
    # deny a fact the console just showed them — say why instead.
    if not is_admin_tier(current_user):
        if not _caller_is_designated_author_of(db, current_user, model):
            if _caller_may_view_model(db, current_user, model):
                raise HTTPException(
                    status_code=403, detail="僅可變更自己註冊的模型端點位址"
                )
            raise HTTPException(status_code=404, detail="模型不存在")

    update_data = request.model_dump(exclude_unset=True)

    if "protocol" in update_data:
        protocol = update_data["protocol"] or "openai_compatible"
        if protocol not in ALLOWED_PROTOCOLS:
            raise HTTPException(
                status_code=422,
                detail=(
                    "protocol 僅支援 openai_compatible 或 triton_grpc;"
                    "custom_adapter 未被 proxy 實作,請勿選用"
                ),
            )

    # Designated non-admin authors: address only. Any other field in the
    # same request is refused — activation, ceiling, department, credentials
    # remain administrator-tier.
    if not is_admin_tier(current_user):
        forbidden = sorted(k for k in update_data if k != "endpoint_url")
        if forbidden or "endpoint_url" not in update_data:
            raise HTTPException(
                status_code=403,
                detail="端點位址設定者僅可變更端點位址",
            )

    # P4.6b: changing the registered address is owner / designated
    # developer only. Omitting the field leaves the row's URL untouched.
    if "endpoint_url" in update_data:
        require_endpoint_address_author(db, current_user)
        if update_data["endpoint_url"] is None:
            raise HTTPException(status_code=400, detail="端點位址不可為空")
        _enforce_endpoint_url(update_data["endpoint_url"])

    # Protocol/URL consistency after both fields (or retained row values) known.
    effective_protocol = (
        update_data["protocol"]
        if "protocol" in update_data
        else (model.protocol or "openai_compatible")
    ) or "openai_compatible"
    effective_url = (
        update_data["endpoint_url"]
        if "endpoint_url" in update_data
        else model.endpoint_url
    )
    if "protocol" in update_data or "endpoint_url" in update_data:
        _enforce_protocol_endpoint(effective_protocol, effective_url)

    # Validate base_model_id if provided
    if "base_model_id" in update_data and update_data["base_model_id"]:
        base = db.query(ModelRegistry).filter(ModelRegistry.id == update_data["base_model_id"]).first()
        if not base:
            raise HTTPException(status_code=400, detail="底層模型不存在")
        if base.id == model_id:
            raise HTTPException(status_code=400, detail="不能將自己設為底層模型")

    # r1_0039: only a *changed* level is probed — re-saving an unrelated
    # field must not fire an outbound call. Probe a shim built from the
    # values about to be written rather than the row itself, so a 422 leaves
    # the session clean (``get_db`` closes without rolling back).
    thinking_probe = None
    if (
        "thinking_effort" in update_data
        and update_data["thinking_effort"] != model.thinking_effort
    ):
        pending_key = update_data.get("api_key")
        thinking_probe = await _run_thinking_probe(
            probe_target=SimpleNamespace(
                id=model.id,
                name=model.name,
                endpoint_url=effective_url,
                api_version=update_data.get("api_version") or model.api_version,
                api_key_secret_ref=(
                    encode_service_token_envelope(str(pending_key).strip())
                    if pending_key is not None and str(pending_key).strip()
                    else model.api_key_secret_ref
                ),
            ),
            level=update_data["thinking_effort"],
            model_type=update_data.get("model_type") or model.model_type,
            protocol=effective_protocol,
        )

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
        actor=current_user,
        action="update",
        resource_type="model",
        resource_id=model.id,
        detail=f"更新模型「{model.display_name}」",
        commit=True,
    )
    response = _build_response(model, caller=current_user, db=db)
    if thinking_probe is not None:
        response["thinking_probe"] = thinking_probe
    return response


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
    if getattr(model, "is_image_primary", False):
        model.is_image_primary = False
    if getattr(model, "is_asr_primary", False):
        model.is_asr_primary = False
    if getattr(model, "is_platform_embedding", False):
        model.is_platform_embedding = False
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
        return _build_response(model, caller=admin, db=db)
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
    return _build_response(model, caller=admin, db=db)


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
    endpoint_url = model.endpoint_url
    model_id = model.id
    display_name = model.display_name
    protocol = model.protocol or "openai_compatible"
    model_name = model.name
    # Release pooled connection before the outbound probe (≤10s).
    db.commit()
    # The model's own gateway key rides the probe (LiteLLM guards /health too).
    probe_key = resolve_model_gateway_key(model)
    status, latency_ms = await probe_model_health_detailed(
        endpoint_url,
        endpoint_kind=ENDPOINT_KIND_MODEL,
        protocol=protocol,
        model_name=model_name,
        api_key=probe_key,
    )
    checked_at = datetime.now(timezone.utc)
    model.health_status = status
    model.health_checked_at = checked_at
    db.commit()
    log_audit_event(
        db,
        actor=admin,
        action="health_check",
        resource_type="model",
        resource_id=model_id,
        status=("failure" if status == HEALTH_UNHEALTHY else "success"),
        detail=f"主動健康檢查: {display_name} → {status} ({latency_ms}ms)",
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
    if not _caller_may_view_model(db, current_user, model):
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
