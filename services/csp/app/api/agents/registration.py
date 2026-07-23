"""Agent registration / CRUD / template download endpoints.

Split verbatim from the former single-module ``app/api/agents.py``
(behavior-preserving refactor).
"""
import io
import os as _os
import zipfile
from datetime import datetime
from pathlib import Path
from anila_security import UnsafeEndpointError, validate_outbound_url
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from anila_contracts import Classification as ClassificationLevel
from app.database import get_db
from app.models.agent import Agent
from app.models.user import User
from app.services.audit_service import log_audit_event
from app.services.agent_readiness import (
    invalidate_trace_evidence,
    manifest_revision,
    manifest_sha256,
)
from app.services.auth_service import (
    get_current_user,
    is_admin_tier,
    require_admin,
)

from app.api.agents._common import (
    _client_ip,
    _require_developer_or_admin,
    validate_agent_manifest,
)
from app.schemas.contracts.agents import (
    REGISTER_DEFAULT_APPROVAL,
    ApprovalStatus,
    RuntimeType,
)


def _enforce_endpoint_url(url: str) -> None:
    """Reject SSRF-prone agent endpoint URLs (loopback / private / metadata).

    ``anila_security.url_guard.validate_outbound_url`` is the same
    helper the ingestion-credentials API uses; agents now share the
    deny-list so a developer can't register an internal-only endpoint and
    have an admin unknowingly approve it.

    Slice 6a (doc 04 §8): validated with ``endpoint_kind="agent"`` — http
    agent endpoints are allowed via ``ANILA_ALLOW_HTTP_AGENT_ENDPOINT``
    (legacy ``ANILA_ALLOW_HTTP_ENDPOINT`` still works with a deprecation
    warning so intranet MLSteam http NodePort agents keep registering).
    """
    try:
        validate_outbound_url(url, endpoint_kind="agent")
    except UnsafeEndpointError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

# This file lives at app/api/agents/registration.py. The official template is
# a monorepo package, so the local/dev fallback must resolve to
# <repo-root>/packages/anila-agent (Compose overrides it with /app/anila-template).
def _resolve_template_dir(source_file: Path | None = None) -> Path:
    """Resolve lazily so a shallow production image path cannot crash import."""
    override = _os.environ.get("ANILA_TEMPLATE_DIR")
    if override:
        return Path(override)

    source = (source_file or Path(__file__)).resolve()
    for parent in source.parents:
        candidate = parent / "packages" / "anila-agent"
        if candidate.is_dir():
            return candidate

    image_default = Path("/app/anila-template")
    if image_default.is_dir():
        return image_default
    # Keep module import safe. The download endpoint returns its existing
    # controlled 404 when the template is genuinely absent.
    return image_default


_TEMPLATE_DIR = _resolve_template_dir()

router = APIRouter()

_IGNORED_TEMPLATE_PARTS = {
    ".git",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
}
_IGNORED_TEMPLATE_SUFFIXES = {".pyc", ".pyo"}


# ── Schemas ──────────────────────────────────────────────────────────────────

class AgentRegisterRequest(BaseModel):
    name: str
    endpoint_url: str
    description_for_router: str
    api_version: str = "v1"
    # Every registered agent must declare the base LLM / VLM it wraps so
    # usage metering can attribute tokens to a real model_registry row.
    # Without this the dashboard's per-model breakdown has phantom
    # "agent X" traffic with no underlying model behind it.
    base_model_id: int = Field(..., description="必須指定底層模型 ID")
    # RAG agents: the single collection this agent's csk- may search (S-Q1).
    # Optional — omit for non-RAG agents. Validated against owner access.
    collection_id: int | None = Field(
        default=None, description="RAG agent 綁定的 collection（其 csk- 僅能搜這一個）"
    )
    capabilities: dict | None = None
    input_schema: dict | None = None
    # doc 05 §3 runtime_type(5 值;預設 openai_compatible_agent = 現況 endpoint proxy)。
    runtime_type: RuntimeType = RuntimeType.OPENAI_COMPATIBLE_AGENT
    classification_ceiling: ClassificationLevel = ClassificationLevel.UNCLASSIFIED
    # doc 05 §4 optional manifest —— 提供則 fail-closed 驗證(422)並留存 manifest_json。
    manifest: dict | None = None
    # doc 06 Phase 1 shadow registration:True → approval_status=draft(盤點暫存);
    # 預設 False = 現況行為(落地 pending_connection_test,第一關 = 連線測試)。
    shadow: bool = False


class AgentResponse(BaseModel):
    id: int
    name: str
    owner_user_id: int
    owner_username: str | None = None
    endpoint_url: str
    api_version: str
    description_for_router: str
    base_model_id: int | None = None
    base_model_name: str | None = None
    bound_collection_id: int | None = None
    capabilities: dict | None = None
    health_status: str
    approval_status: str
    requires_encryption: bool = False
    # doc 05 §3/§4/§6 registry-upgrade fields (Slice 5a). Optional so existing
    # consumers keep working; surfaced for the developer/admin registry UI.
    runtime_type: str | None = None
    agent_version: str | None = None
    audit_level: str | None = None
    # NULL = unset (UI「未設定（不可派工）」); omitted on create defaults to 無機密.
    # Persistable, but runtime admission always fail-closed on null.
    classification_ceiling: ClassificationLevel | None = None
    default_classification_level: str | None = None
    manifest_json: dict | None = None
    trace_test_passed_at: datetime | None = None
    health_checked_at: datetime | None = None
    manifest_sha256: str | None = None
    manifest_revision: str | None = None
    trace_test_governance_fingerprint: str | None = None
    # Sprint 13 PR A3 — admin-editable runtime knobs (tool permissions,
    # workspace caps, guardrails). NULL means "agent uses code defaults".
    runtime_config: dict | None = None
    # Ceiling gate only: null ceiling is never dispatchable. Full readiness
    # (approval/health/trace) is evaluated separately by agent_readiness.
    dispatchable: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}


# Agents and Models track health status in different vocabularies —
# Agent uses "healthy"/"unhealthy"/"unknown", ModelRegistry uses
# "online"/"offline"/"connecting". The background health_checker writes
# the ModelRegistry vocabulary into Agent rows, so both sets can coexist
# in the DB. Normalize on read so the admin UI sees a single vocabulary
# and "HEALTHY" count in the dashboard matches the table rows.
_AGENT_HEALTH_MAP = {
    "online": "healthy",
    "healthy": "healthy",
    "connecting": "unknown",
    "unknown": "unknown",
    "offline": "unhealthy",
    "unhealthy": "unhealthy",
}


def _optional_classification_ceiling(agent: Agent) -> str | None:
    """Serialize ceiling; ``None`` means unset / not dispatchable."""
    raw = getattr(agent, "classification_ceiling", None)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise RuntimeError(
            "agents.classification_ceiling must be a canonical value or null"
        )
    return ClassificationLevel.from_storage(raw).to_storage()


def _serialize_agent(agent: Agent) -> dict:
    raw = agent.health_status or "unknown"
    normalized = _AGENT_HEALTH_MAP.get(raw, raw)
    owner = getattr(agent, "owner", None)
    base = getattr(agent, "base_model", None)
    ceiling = _optional_classification_ceiling(agent)
    return {
        "id": agent.id,
        "name": agent.name,
        "owner_user_id": agent.owner_user_id,
        "owner_username": owner.username if owner else None,
        "endpoint_url": agent.endpoint_url,
        "api_version": agent.api_version,
        "description_for_router": agent.description_for_router,
        "base_model_id": agent.base_model_id,
        "base_model_name": base.display_name if base else None,
        "bound_collection_id": getattr(agent, "bound_collection_id", None),
        "capabilities": agent.capabilities,
        "health_status": normalized,
        "approval_status": agent.approval_status,
        "requires_encryption": bool(getattr(agent, "requires_encryption", False)),
        "runtime_type": getattr(agent, "runtime_type", None),
        "agent_version": getattr(agent, "agent_version", None),
        "audit_level": getattr(agent, "audit_level", None),
        "classification_ceiling": ceiling,
        # Null ceiling is saveable but never dispatchable (fail-closed).
        "dispatchable": ceiling is not None,
        "default_classification_level": getattr(
            agent, "default_classification_level", None
        ),
        "manifest_json": getattr(agent, "manifest_json", None),
        "trace_test_passed_at": getattr(agent, "trace_test_passed_at", None),
        "health_checked_at": getattr(agent, "health_checked_at", None),
        "manifest_sha256": getattr(agent, "manifest_sha256", None),
        "manifest_revision": getattr(agent, "manifest_revision", None),
        "trace_test_governance_fingerprint": getattr(
            agent, "trace_test_governance_fingerprint", None
        ),
        "runtime_config": getattr(agent, "runtime_config", None),
        "created_at": agent.created_at,
    }


class AgentUpdateRequest(BaseModel):
    """Owner / admin-editable fields. Intentionally omits:
    - ``name`` (agent_id referenced by every registered client — immutable)
    - ``approval_status`` (dedicated /approve + /reject admin endpoints)
    - ``requires_encryption`` (dedicated /encryption admin endpoint)
    - ``owner_user_id`` (transfer of ownership isn't exposed yet)
    """
    endpoint_url: str | None = None
    api_version: str | None = None
    description_for_router: str | None = None
    base_model_id: int | None = None
    capabilities: dict | None = None
    input_schema: dict | None = None
    # doc 05 §4 — replace the stored manifest snapshot (validated fail-closed).
    manifest: dict | None = None
    classification_ceiling: ClassificationLevel | None = None
    default_classification_level: ClassificationLevel | None = None
    audit_level: str | None = None
    trace_callback_mode: str | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _should_include_template_path(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if any(part in _IGNORED_TEMPLATE_PARTS for part in relative.parts):
        return False
    if path.suffix in _IGNORED_TEMPLATE_SUFFIXES:
        return False
    return path.is_file()


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/template/download")
def download_template(
    current_user: User = Depends(_require_developer_or_admin),
) -> StreamingResponse:
    """Serve the official anila-core template mirroring the AgenticRAG project."""
    buf = io.BytesIO()
    template_dir = _TEMPLATE_DIR
    if not template_dir.exists():
        raise HTTPException(status_code=404, detail="Template not found on server")

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(template_dir.rglob("*")):
            if _should_include_template_path(path, template_dir):
                arcname = "anila-core-template/" + path.relative_to(template_dir).as_posix()
                zf.write(path, arcname)

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=anila-core-template.zip"},
    )


@router.post("/register", response_model=AgentResponse)
def register_agent(
    request: AgentRegisterRequest,
    http_request: Request,
    current_user: User = Depends(_require_developer_or_admin),
    db: Session = Depends(get_db),
):
    existing = db.query(Agent).filter(Agent.name == request.name).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Agent 名稱「{request.name}」已存在")

    # SSRF guard — block loopback / private / cloud-metadata endpoints
    # before they ever land in the DB. Same helper the ingestion
    # credentials API uses (anila_security.url_guard).
    _enforce_endpoint_url(request.endpoint_url)

    # Validate base model — a registered agent must wrap a real, active
    # model_registry row so per-model usage accounting stays truthful.
    from app.models.model_registry import ModelRegistry
    base = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.id == request.base_model_id)
        .first()
    )
    if base is None:
        raise HTTPException(
            status_code=400,
            detail=f"底層模型 id={request.base_model_id} 不存在",
        )
    if not base.is_active:
        raise HTTPException(
            status_code=400,
            detail=f"底層模型「{base.display_name}」已停用，請挑選已啟用的模型",
        )

    # RAG agents: bind a single collection the agent's csk- may search.
    # Validate the registering owner actually has access to it (admin or
    # owner) so an agent can't be bound to a collection its owner can't see.
    if request.collection_id is not None:
        from app.api.ingestion.collections import _require_collection_access
        _require_collection_access(db, current_user, request.collection_id)

    # doc 05 §4 — optional manifest is validated fail-closed (422) and the
    # normalized snapshot is stored so the registry has the formal schema
    # (not just the loose ``capabilities`` blob).
    manifest_json = (
        validate_agent_manifest(request.manifest)
        if request.manifest is not None
        else None
    )
    canonical_manifest = None
    if manifest_json is not None:
        # ``validate_agent_manifest`` accepts a legacy adapter for old clients;
        # only the shared anila-contracts v2 object receives authority hashes.
        from anila_contracts import AgentManifest as CanonicalAgentManifest

        try:
            canonical_manifest = CanonicalAgentManifest.model_validate(manifest_json)
        except Exception:
            canonical_manifest = None

    # doc 06 Phase 1: shadow inventory rows land as ``draft``; the normal path
    # lands at the first gate (pending_connection_test = 現況 pending 等價)。
    approval_status = (
        ApprovalStatus.DRAFT.value if request.shadow else REGISTER_DEFAULT_APPROVAL
    )

    agent = Agent(
        name=request.name,
        owner_user_id=current_user.id,
        endpoint_url=request.endpoint_url,
        description_for_router=request.description_for_router,
        base_model_id=request.base_model_id,
        bound_collection_id=request.collection_id,
        capabilities=request.capabilities,
        input_schema=request.input_schema,
        # Once a canonical manifest is supplied, its protocol identity is the
        # authority.  Keep the denormalised CSP row aligned so later readiness
        # checks can detect direct DB/config drift instead of silently trusting
        # two divergent declarations.
        runtime_type=(
            canonical_manifest.runtime_type.value
            if canonical_manifest
            else request.runtime_type.value
        ),
        agent_version=(canonical_manifest.version if canonical_manifest else None),
        api_version=(canonical_manifest.api_version if canonical_manifest else request.api_version),
        classification_ceiling=request.classification_ceiling.to_storage(),
        manifest_json=manifest_json,
        manifest_sha256=(manifest_sha256(canonical_manifest) if canonical_manifest else None),
        manifest_revision=(manifest_revision(canonical_manifest) if canonical_manifest else None),
        default_classification_level=(
            canonical_manifest.classification.default.to_storage()
            if canonical_manifest
            else (
                str(manifest_json.get("classification", {}).get("default"))
                if isinstance(manifest_json, dict)
                and isinstance(manifest_json.get("classification"), dict)
                and manifest_json["classification"].get("default") is not None
                else request.classification_ceiling.to_storage()
            )
        ),
        trace_callback_mode=(
            "sse_and_post"
            if canonical_manifest and "step-event/v1" in canonical_manifest.event_protocols
            else (
                manifest_json.get("trace", {}).get("callback_mode")
                if isinstance(manifest_json, dict)
                and isinstance(manifest_json.get("trace"), dict)
                else None
            )
        ),
        approval_status=approval_status,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    log_audit_event(
        db, actor=current_user, action="register", resource_type="agent",
        resource_id=agent.id, detail=f"註冊 agent「{agent.name}」",
        ip_address=_client_ip(http_request), commit=True,
    )
    return _serialize_agent(agent)


@router.get("", response_model=list[AgentResponse])
def list_agents(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # admin + owner 都應該看到所有 agent。先前單獨檢查 "admin" 會把
    # owner 推到 else 的 owner_user_id 過濾,讓 owner 看不到不是自己
    # 註冊的 agent — 造成「CSP UI 顯示 2 個、ANILA UI 顯示 3 個」的
    # 錯覺,以為刪除沒同步,實際上 agent 還在 DB,只是被 UI 藏起來。
    if is_admin_tier(current_user):
        agents = db.query(Agent).order_by(Agent.created_at.desc()).all()
    else:
        agents = db.query(Agent).filter(Agent.owner_user_id == current_user.id).all()
    return [_serialize_agent(a) for a in agents]


@router.get("/{agent_id}", response_model=AgentResponse)
def get_agent(
    agent_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    if not is_admin_tier(current_user) and agent.owner_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="無權限查看此 Agent")
    return _serialize_agent(agent)


@router.put("/{agent_id}", response_model=AgentResponse)
def update_agent(
    agent_id: int,
    payload: AgentUpdateRequest,
    http_request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Owner / admin may edit description, endpoint, capabilities, etc.

    Delete stays admin-only (``DELETE /api/agents/{id}``), so developers
    get self-service maintenance of their own agent's metadata without
    ever being able to remove it — which would orphan historical
    conversations and audit entries that reference the agent by id.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    if not is_admin_tier(current_user) and agent.owner_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="無權限編輯此 Agent")

    patch = payload.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(status_code=400, detail="沒有提供要更新的欄位")

    # doc 05 §4 — a submitted manifest is validated fail-closed (422) and
    # mapped onto ``manifest_json`` (the ``manifest`` request field is not a
    # column). Explicit ``null`` clears the stored snapshot.
    if "manifest" in patch:
        raw_manifest = patch.pop("manifest")
        patch["manifest_json"] = (
            validate_agent_manifest(raw_manifest) if raw_manifest is not None else None
        )
        if raw_manifest is None:
            patch["manifest_sha256"] = None
            patch["manifest_revision"] = None
        else:
            from anila_contracts import AgentManifest as CanonicalAgentManifest

            try:
                canonical = CanonicalAgentManifest.model_validate(patch["manifest_json"])
            except Exception:
                canonical = None
            patch["manifest_sha256"] = manifest_sha256(canonical) if canonical else None
            patch["manifest_revision"] = (
                manifest_revision(canonical) if canonical else None
            )
            if canonical:
                patch.setdefault(
                    "default_classification_level",
                    canonical.classification.default.to_storage(),
                )
                patch.setdefault("agent_version", canonical.version)
                patch.setdefault("runtime_type", canonical.runtime_type.value)
                patch.setdefault("api_version", canonical.api_version)
                patch.setdefault(
                    "trace_callback_mode",
                    "sse_and_post"
                    if "step-event/v1" in canonical.event_protocols
                    else None,
                )

    # classification_ceiling: explicit null is legal 「未設定」 (persistable,
    # never dispatchable). default_classification_level remains required when
    # present — null is rejected.
    if "classification_ceiling" in patch:
        value = patch["classification_ceiling"]
        if value is None:
            patch["classification_ceiling"] = None
        else:
            patch["classification_ceiling"] = (
                value.to_storage()
                if hasattr(value, "to_storage")
                else ClassificationLevel.from_storage(str(value)).to_storage()
            )
    if "default_classification_level" in patch:
        if patch["default_classification_level"] is None:
            raise HTTPException(
                status_code=400, detail="default_classification_level 不可設為空值"
            )
        value = patch["default_classification_level"]
        patch["default_classification_level"] = (
            value.to_storage()
            if hasattr(value, "to_storage")
            else ClassificationLevel.from_storage(str(value)).to_storage()
        )
    # 不變式:預設分級 ≤ 分類上限(以 patch 後有效值對比較)。
    # null ceiling = unset / not dispatchable → skip ceiling comparison.
    if "classification_ceiling" in patch or "default_classification_level" in patch:
        effective_ceiling = patch.get(
            "classification_ceiling",
            getattr(agent, "classification_ceiling", None),
        )
        effective_default = patch.get(
            "default_classification_level",
            getattr(agent, "default_classification_level", None),
        )
        if effective_ceiling is not None and effective_default is not None:
            ceiling_lvl = ClassificationLevel.from_storage(str(effective_ceiling))
            default_lvl = ClassificationLevel.from_storage(str(effective_default))
            if default_lvl > ceiling_lvl:
                raise HTTPException(
                    status_code=422,
                    detail="預設分級不可高於分類上限",
                )
    if "audit_level" in patch and patch["audit_level"] not in {"full_trace"}:
        raise HTTPException(status_code=400, detail="audit_level 必須是 full_trace")
    if "trace_callback_mode" in patch and patch["trace_callback_mode"] not in {
        "sse_and_post", "sse", "post"
    }:
        raise HTTPException(status_code=400, detail="trace_callback_mode 無效")

    # SSRF guard — endpoint_url 變更時重新驗證；同時把 approval_status 退回
    # pending，避免 owner 把已核可 agent 的 endpoint 改到內網（H4）。
    endpoint_changed = (
        "endpoint_url" in patch
        and patch["endpoint_url"] is not None
        and patch["endpoint_url"] != agent.endpoint_url
    )
    if endpoint_changed:
        _enforce_endpoint_url(patch["endpoint_url"])

    # If the caller is replacing base_model_id, keep the same invariant
    # the register endpoint enforces: the new id must point at an active
    # model. base_model_id itself is required on the model (not nullable
    # from the UI side), so reject explicit nulls too.
    if "base_model_id" in patch:
        new_id = patch["base_model_id"]
        if new_id is None:
            raise HTTPException(status_code=400, detail="底層模型不可設為空值")
        from app.models.model_registry import ModelRegistry
        base = db.query(ModelRegistry).filter(ModelRegistry.id == new_id).first()
        if base is None:
            raise HTTPException(status_code=400, detail=f"底層模型 id={new_id} 不存在")
        if not base.is_active:
            raise HTTPException(
                status_code=400,
                detail=f"底層模型「{base.display_name}」已停用",
            )

    changed: list[str] = []
    for field, value in patch.items():
        if getattr(agent, field) != value:
            setattr(agent, field, value)
            changed.append(field)
    if not changed:
        return _serialize_agent(agent)

    # 任何治理欄位變更都會強制重新核可，避免「核可一次後 owner 改成內網」的
    # bypass。admin 變更自己的 agent 也一樣 — 規則一致才好稽核。doc 05 §6:
    # 端點換過後,舊的 Full Trace 落章作廢 —— 清 trace_test_passed_at/report,
    # 退回第一關 pending_connection_test,重新走 connection→trace→review。
    governance_changed = bool(
        set(changed)
        & {
            "endpoint_url",
            "api_version",
            "base_model_id",
            "classification_ceiling",
            "default_classification_level",
            "audit_level",
            "trace_callback_mode",
            "manifest_json",
            "manifest_sha256",
            "manifest_revision",
        }
    )
    if governance_changed:
        invalidate_trace_evidence(agent)
        changed.append("approval_status->pending_connection_test")

    db.commit()
    db.refresh(agent)
    log_audit_event(
        db,
        actor=current_user,
        action="update",
        resource_type="agent",
        resource_id=agent.id,
        detail=f"更新 agent「{agent.name}」：" + ", ".join(changed),
        ip_address=_client_ip(http_request),
        commit=True,
    )
    return _serialize_agent(agent)


@router.delete("/{agent_id}")
def delete_agent(
    agent_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    agent_name = agent.name
    db.delete(agent)
    db.commit()
    log_audit_event(
        db, actor=admin, action="delete", resource_type="agent",
        resource_id=agent_id, detail=f"刪除 agent「{agent_name}」",
        ip_address=_client_ip(request), commit=True,
    )
    return {"message": f"已刪除 agent「{agent_name}」"}
