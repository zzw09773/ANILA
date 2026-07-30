"""Agent registration / CRUD / template download endpoints.

Split verbatim from the former single-module ``app/api/agents.py``
(behavior-preserving refactor).
"""
import io
import os as _os
import zipfile
from datetime import datetime
from pathlib import Path
from anila_core.security import UnsafeEndpointError, validate_outbound_url
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.agent import Agent
from app.models.user import User
from app.services.audit_service import log_audit_event
from app.services.auth_service import (
    get_current_user,
    is_admin_tier,
    require_admin,
)

from app.api.agents._common import (
    _client_ip,
    _require_agent_editor,
    _require_developer_or_admin,
    apply_default_classification_level,
    parse_stored_classification_level,
    refuse_classification_downgrade,
    requires_controlled_access,
    validate_agent_manifest,
)
from app.schemas.contracts.agents import (
    REGISTER_DEFAULT_APPROVAL,
    ApprovalStatus,
    RuntimeType,
)
from app.schemas.contracts.classification import ClassificationLevel


def _enforce_endpoint_url(url: str) -> None:
    """Reject SSRF-prone agent endpoint URLs (loopback / private / metadata).

    ``anila_core.security.url_guard.validate_outbound_url`` is the same
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

# One extra ``.parent`` vs the old app/api/agents.py: this file lives one
# package level deeper (app/api/agents/registration.py), and the default
# must keep pointing at <repo-root>/anila-agent.
_TEMPLATE_DIR = Path(
    _os.environ.get(
        "ANILA_TEMPLATE_DIR",
        str(Path(__file__).parent.parent.parent.parent.parent.parent / "anila-agent"),
    )
)

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
    # doc 05 §4 optional manifest —— 提供則 fail-closed 驗證(422)並留存 manifest_json。
    manifest: dict | None = None
    # doc 06 Phase 1 shadow registration:True → approval_status=draft(盤點暫存);
    # 預設 False = 現況行為(落地 pending_connection_test,第一關 = 連線測試)。
    shadow: bool = False
    # G9: developer chooses the project's classification level at register time.
    # Stored in ``default_classification_level``; ``requires_encryption`` is
    # derived (level >= 密). Unknown values → 422 via ClassificationLevel.
    default_classification_level: ClassificationLevel = ClassificationLevel.UNCLASSIFIED


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
    classification_ceiling: str | None = None
    default_classification_level: str | None = None
    manifest_json: dict | None = None
    trace_test_passed_at: datetime | None = None
    # Sprint 13 PR A3 — admin-editable runtime knobs (tool permissions,
    # workspace caps, guardrails). NULL means "agent uses code defaults".
    runtime_config: dict | None = None
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


def _serialize_agent(agent: Agent) -> dict:
    raw = agent.health_status or "unknown"
    normalized = _AGENT_HEALTH_MAP.get(raw, raw)
    owner = getattr(agent, "owner", None)
    base = getattr(agent, "base_model", None)
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
        "classification_ceiling": getattr(agent, "classification_ceiling", None),
        "default_classification_level": getattr(
            agent, "default_classification_level", None
        ),
        "manifest_json": getattr(agent, "manifest_json", None),
        "trace_test_passed_at": getattr(agent, "trace_test_passed_at", None),
        "runtime_config": getattr(agent, "runtime_config", None),
        "created_at": agent.created_at,
    }


class AgentUpdateRequest(BaseModel):
    """Owner / admin-editable fields. Intentionally omits:
    - ``name`` (agent_id referenced by every registered client — immutable)
    - ``approval_status`` (dedicated /approve + /reject admin endpoints)
    - ``owner_user_id`` (transfer of ownership isn't exposed yet)

    ``default_classification_level`` may also be set via the dedicated
    ``POST /api/agents/{id}/classification`` endpoint; both paths derive
    ``requires_encryption`` from the level (mirror threshold ≥ 密).
    """
    endpoint_url: str | None = None
    api_version: str | None = None
    description_for_router: str | None = None
    base_model_id: int | None = None
    capabilities: dict | None = None
    input_schema: dict | None = None
    # doc 05 §4 — replace the stored manifest snapshot (validated fail-closed).
    manifest: dict | None = None
    default_classification_level: ClassificationLevel | None = None


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
    # credentials API uses (anila_core.security.url_guard).
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

    # doc 06 Phase 1: shadow inventory rows land as ``draft``; the normal path
    # lands at the first gate (pending_connection_test = 現況 pending 等價)。
    approval_status = (
        ApprovalStatus.DRAFT.value if request.shadow else REGISTER_DEFAULT_APPROVAL
    )

    level = request.default_classification_level
    agent = Agent(
        name=request.name,
        owner_user_id=current_user.id,
        endpoint_url=request.endpoint_url,
        api_version=request.api_version,
        description_for_router=request.description_for_router,
        base_model_id=request.base_model_id,
        bound_collection_id=request.collection_id,
        capabilities=request.capabilities,
        input_schema=request.input_schema,
        runtime_type=request.runtime_type.value,
        manifest_json=manifest_json,
        approval_status=approval_status,
        default_classification_level=level.to_storage(),
        requires_encryption=requires_controlled_access(level),
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    log_audit_event(
        db, actor=current_user, action="register", resource_type="agent",
        resource_id=agent.id,
        detail=(
            f"註冊 agent「{agent.name}」,預設分類等級={level.to_storage()}"
        ),
        ip_address=_client_ip(http_request),
        metadata={
            "default_classification_level": level.to_storage(),
            "requires_controlled_access": agent.requires_encryption,
        },
        commit=True,
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
    _require_agent_editor(agent, current_user)

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

    # Classification level is applied via the shared helper so the legacy
    # boolean stays derived from the same mirror threshold as conversations.
    # Developers may not lower the effective policy level (incl. legacy floor).
    # Audit is driven by the *effective* transition (not stored-column alone).
    level_change: (
        tuple[
            ClassificationLevel,
            ClassificationLevel,
            ClassificationLevel,
            ClassificationLevel,
        ]
        | None
    ) = None
    level_fields_touched = False
    if "default_classification_level" in patch:
        raw_level = patch.pop("default_classification_level")
        if raw_level is None:
            raise HTTPException(
                status_code=400, detail="預設分類等級不可設為空值"
            )
        new_level = (
            raw_level
            if isinstance(raw_level, ClassificationLevel)
            else parse_stored_classification_level(raw_level)
        )
        refuse_classification_downgrade(agent, new_level, current_user)
        effective_transition, changed, stored_transition = (
            apply_default_classification_level(agent, new_level)
        )
        if changed:
            level_fields_touched = True
            if effective_transition is not None:
                from_eff, to_eff = effective_transition
                from_stored, to_stored = stored_transition
                level_change = (from_eff, to_eff, from_stored, to_stored)

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

    changed_level_fields = (
        ["default_classification_level", "requires_encryption"]
        if level_fields_touched
        else []
    )
    changed: list[str] = list(changed_level_fields)
    for field, value in patch.items():
        if getattr(agent, field) != value:
            setattr(agent, field, value)
            changed.append(field)
    if not changed:
        return _serialize_agent(agent)

    # 任何端點變更都會強制重新核可，避免「核可一次後 owner 改成內網」的
    # bypass。admin 變更自己的 agent 也一樣 — 規則一致才好稽核。doc 05 §6:
    # 端點換過後,舊的 Full Trace 落章作廢 —— 清 trace_test_passed_at/report,
    # 退回第一關 pending_connection_test,重新走 connection→trace→review。
    reapproval_required = (
        endpoint_changed and agent.approval_status == ApprovalStatus.APPROVED.value
    )
    if reapproval_required:
        agent.approval_status = REGISTER_DEFAULT_APPROVAL
        agent.approved_by = None
        agent.approved_at = None
        agent.trace_test_passed_at = None
        agent.trace_test_report = None
        changed.append("approval_status->pending_connection_test")

    # Governance convention: audit in the same transaction, check result,
    # abort with 500 before commit if the audit row could not be written.
    # Classification audit when the *effective* policy level changed
    # (legacy-floor drop included); boolean-only same-effective repair
    # commits without a set_classification row.
    if level_change is not None:
        from_eff, to_eff, from_stored, to_stored = level_change
        level_audit = log_audit_event(
            db,
            actor=current_user,
            action="set_classification",
            resource_type="agent",
            resource_id=agent.id,
            detail=(
                f"變更 agent「{agent.name}」有效分類等級："
                f"{from_eff.to_storage()} → {to_eff.to_storage()}"
            ),
            ip_address=_client_ip(http_request),
            metadata={
                "from_level": from_eff.to_storage(),
                "to_level": to_eff.to_storage(),
                "from_stored_level": from_stored.to_storage(),
                "to_stored_level": to_stored.to_storage(),
                "requires_controlled_access": agent.requires_encryption,
            },
            commit=False,
        )
        if level_audit is None:
            raise HTTPException(
                status_code=500,
                detail="稽核紀錄寫入失敗，分類等級變更未生效",
            )
    other_changed = [f for f in changed if f not in changed_level_fields]
    if other_changed:
        update_audit = log_audit_event(
            db,
            actor=current_user,
            action="update",
            resource_type="agent",
            resource_id=agent.id,
            detail=f"更新 agent「{agent.name}」：" + ", ".join(changed),
            ip_address=_client_ip(http_request),
            commit=False,
        )
        if update_audit is None:
            raise HTTPException(
                status_code=500,
                detail="稽核紀錄寫入失敗，agent 更新未生效",
            )

    db.commit()
    db.refresh(agent)
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
