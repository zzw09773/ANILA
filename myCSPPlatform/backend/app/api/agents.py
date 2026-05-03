"""Agent management API (JWT-protected control plane).

Credential tier: JWT access token for all endpoints here.
Data plane list endpoint (GET /v1/agents, API Key auth) lives in proxy.py.
"""
import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path
import httpx
from anila_core.security import UnsafeEndpointError, validate_outbound_url
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.agent import Agent, UserAgentPermission
from app.models.user import User
from app.services.audit_service import log_audit_event
from app.services.auth_service import (
    get_current_user,
    require_admin,
    verify_service_token,
)


def _enforce_endpoint_url(url: str) -> None:
    """Reject SSRF-prone agent endpoint URLs (loopback / private / metadata).

    ``anila_core.security.url_guard.validate_outbound_url`` is the same
    helper the ingestion-credentials API uses; agents now share the
    deny-list so a developer can't register an internal-only endpoint and
    have an admin unknowingly approve it.
    """
    try:
        validate_outbound_url(url)
    except UnsafeEndpointError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

import os as _os
_TEMPLATE_DIR = Path(
    _os.environ.get(
        "ANILA_TEMPLATE_DIR",
        str(Path(__file__).parent.parent.parent.parent.parent / "AgenticRAG"),
    )
)

router = APIRouter(prefix="/api/agents", tags=["Agent 管理"])

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
    capabilities: dict | None = None
    input_schema: dict | None = None


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
    capabilities: dict | None = None
    health_status: str
    approval_status: str
    requires_encryption: bool = False
    # Sprint 13 PR A3 — admin-editable runtime knobs (tool permissions,
    # workspace caps, guardrails). NULL means "agent uses code defaults".
    runtime_config: dict | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AgentRuntimeConfigUpdate(BaseModel):
    """PATCH payload for ``runtime_config``.

    Setting ``runtime_config`` to ``None`` clears the override (agent
    falls back to code defaults). An explicit empty dict ``{}`` means
    "admin set empty" — different semantics from ``None``.
    """

    runtime_config: dict | None = None


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
        "capabilities": agent.capabilities,
        "health_status": normalized,
        "approval_status": agent.approval_status,
        "requires_encryption": bool(getattr(agent, "requires_encryption", False)),
        "runtime_config": getattr(agent, "runtime_config", None),
        "created_at": agent.created_at,
    }


class AgentEncryptionUpdate(BaseModel):
    requires_encryption: bool


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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _require_developer_or_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role not in ("admin", "developer"):
        raise HTTPException(status_code=403, detail="需要開發者或管理員權限")
    return current_user


def _client_ip(request: Request | None) -> str | None:
    """Extract caller IP from the Request — tolerant of reverse-proxy
    setups (reads X-Forwarded-For first hop) and of None so endpoints
    that don't inject a Request stay safe."""
    if request is None:
        return None
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


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

    agent = Agent(
        name=request.name,
        owner_user_id=current_user.id,
        endpoint_url=request.endpoint_url,
        api_version=request.api_version,
        description_for_router=request.description_for_router,
        base_model_id=request.base_model_id,
        capabilities=request.capabilities,
        input_schema=request.input_schema,
        approval_status="pending",
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
    if current_user.role == "admin":
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
    if current_user.role != "admin" and agent.owner_user_id != current_user.id:
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
    if current_user.role != "admin" and agent.owner_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="無權限編輯此 Agent")

    patch = payload.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(status_code=400, detail="沒有提供要更新的欄位")

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

    # 任何端點變更都會強制重新核可，避免「核可一次後 owner 改成內網」的
    # bypass。admin 變更自己的 agent 也一樣 — 規則一致才好稽核。
    reapproval_required = endpoint_changed and agent.approval_status == "approved"
    if reapproval_required:
        agent.approval_status = "pending"
        agent.approved_by = None
        agent.approved_at = None
        changed.append("approval_status->pending")

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


@router.post("/{agent_id}/approve")
def approve_agent(
    agent_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    if agent.approval_status == "approved":
        return {"message": f"Agent「{agent.name}」已是核准狀態"}
    agent.approval_status = "approved"
    agent.approved_by = admin.id
    agent.approved_at = datetime.now(timezone.utc)
    db.commit()
    log_audit_event(
        db, actor=admin, action="approve", resource_type="agent",
        resource_id=agent.id, detail=f"核准 agent「{agent.name}」",
        ip_address=_client_ip(request), commit=True,
    )
    return {"message": f"已核准 agent「{agent.name}」"}


@router.get("/{agent_id}/runtime-config")
def get_agent_runtime_config(
    agent_id: int,
    current_user: User = Depends(_require_developer_or_admin),
    db: Session = Depends(get_db),
):
    """Sprint 13 PR A3 — read the agent's persisted runtime config.

    Used by:
      * the CSP admin UI ``AgentRuntimeConfigView.vue`` to populate the
        permission / workspace / guardrails tabs;
      * the agent process itself (Sprint 13 PR A4) which polls every
        30 s for hot-reload — that path uses the agent's own service
        token, not a developer/admin token, so future work may add a
        token-class check; for now any developer/admin can read it.

    Returns ``{"runtime_config": dict | None}``. NULL means the agent
    falls back to its hard-coded defaults.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    if current_user.role != "admin" and agent.owner_user_id != current_user.id:
        raise HTTPException(
            status_code=403, detail="只有 agent 擁有者或管理員可讀取此設定",
        )
    return {
        "agent_id": agent.id,
        "agent_name": agent.name,
        "runtime_config": agent.runtime_config,
    }


@router.patch("/{agent_id}/runtime-config")
def patch_agent_runtime_config(
    agent_id: int,
    payload: AgentRuntimeConfigUpdate,
    request: Request,
    current_user: User = Depends(_require_developer_or_admin),
    db: Session = Depends(get_db),
):
    """Sprint 13 PR A3 — write per-agent runtime config.

    PATCH semantics: the body's ``runtime_config`` value REPLACES the
    stored value (no deep-merge). Pass ``None`` to clear the override
    so the agent reverts to code defaults; pass ``{}`` to enforce
    "explicit empty" semantics (cleared permission lists, no
    guardrails). Audit logged.

    Validation here is intentionally loose — the column accepts any
    JSON shape because admins may set keys the deployed agent code
    doesn't recognise yet (forward-compat). The agent-side parser
    (PR A4) is responsible for tolerating unknown keys.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    if current_user.role != "admin" and agent.owner_user_id != current_user.id:
        raise HTTPException(
            status_code=403, detail="只有 agent 擁有者或管理員可變更此設定",
        )

    agent.runtime_config = payload.runtime_config
    db.commit()
    db.refresh(agent)

    log_audit_event(
        db, actor=current_user, action="set_runtime_config",
        resource_type="agent", resource_id=agent.id,
        detail=(
            f"更新 agent「{agent.name}」runtime_config "
            f"({'cleared' if payload.runtime_config is None else 'set'})"
        ),
        ip_address=_client_ip(request), commit=True,
    )

    return {
        "agent_id": agent.id,
        "agent_name": agent.name,
        "runtime_config": agent.runtime_config,
    }


@router.post("/{agent_id}/encryption")
def set_agent_encryption(
    agent_id: int,
    payload: AgentEncryptionUpdate,
    http_request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    agent.requires_encryption = payload.requires_encryption
    db.commit()
    db.refresh(agent)
    log_audit_event(
        db, actor=admin, action="set_encryption", resource_type="agent",
        resource_id=agent.id,
        detail=f"{'啟用' if payload.requires_encryption else '停用'} agent「{agent.name}」加密模式",
        ip_address=_client_ip(http_request), commit=True,
    )
    return {
        "message": f"已更新 agent「{agent.name}」的加密設定",
        "requires_encryption": agent.requires_encryption,
    }


@router.post("/{agent_id}/health-check")
async def trigger_agent_health_check(
    agent_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Probe an agent's endpoint and update ``health_status``.

    Mirrors ``POST /api/models/{id}/health-check`` for parity on the
    management UI: the admin clicks "檢查", the backend tries a few
    common liveness paths, and the DB stamp is updated so the colored
    dot in the agents list reflects reality.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 不存在")

    ip = _client_ip(request)
    probe_paths = ["/health", "/v1/models", "/"]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for path in probe_paths:
                try:
                    resp = await client.get(
                        f"{agent.endpoint_url.rstrip('/')}{path}"
                    )
                    if resp.status_code < 500:
                        agent.health_status = "healthy"
                        db.commit()
                        log_audit_event(
                            db, actor=admin, action="health_check",
                            resource_type="agent", resource_id=agent.id,
                            detail=f"手動健康檢查成功: {agent.name}",
                            ip_address=ip,
                            commit=True,
                        )
                        return {
                            "status": "healthy",
                            "detail": f"端點 {path} 回應 {resp.status_code}",
                        }
                except httpx.ConnectError:
                    continue
            agent.health_status = "unhealthy"
            db.commit()
            log_audit_event(
                db, actor=admin, action="health_check",
                resource_type="agent", resource_id=agent.id,
                detail=f"手動健康檢查離線: {agent.name}",
                ip_address=ip,
                commit=True,
            )
            return {"status": "unhealthy", "detail": "無法連線到 agent 端點"}
    except Exception as e:
        agent.health_status = "unhealthy"
        db.commit()
        log_audit_event(
            db, actor=admin, action="health_check",
            resource_type="agent", resource_id=agent.id,
            status="failure",
            detail=f"手動健康檢查失敗: {agent.name} ({e})",
            ip_address=ip,
            commit=True,
        )
        return {"status": "unhealthy", "detail": str(e)}


@router.post("/{agent_id}/reject")
def reject_agent(
    agent_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    agent.approval_status = "rejected"
    agent.approved_by = admin.id
    agent.approved_at = datetime.now(timezone.utc)
    db.commit()
    log_audit_event(
        db, actor=admin, action="reject", resource_type="agent",
        resource_id=agent.id, detail=f"拒絕 agent「{agent.name}」",
        ip_address=_client_ip(request), commit=True,
    )
    return {"message": f"已拒絕 agent「{agent.name}」"}


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


# ── Sprint 8 X / Phase A — service token bootstrap & credentials ─────────────
#
# Six new endpoints sit on top of the ``agent_credentials`` table:
#
#   POST /api/agents/{id}/issue-bootstrap        admin → bsk- token (one-shot)
#   POST /api/agents/{id}/bootstrap              caller → exchange bsk- for csk-
#   POST /api/agents/{id}/credentials/issue-static  admin (Phase F Tier 0)
#   GET  /api/agents/{id}/credentials            admin → list active credentials
#   POST /api/agents/{id}/credentials/{cid}/rotate  admin → rotate one credential
#   DELETE /api/agents/{id}/credentials/{cid}    admin → revoke one credential
#
# Plus one self-service endpoint for Tier 1 polling agents:
#
#   GET /api/agents/{id}/credentials/me          agent (auth = service token)

from app.models.agent_credential import AgentCredential
from app.services import agent_credential_service
from app.services.proxy_service import invalidate_agent_token_cache


# ---- Schemas ---------------------------------------------------------------


class IssueBootstrapRequest(BaseModel):
    ttl_seconds: int = Field(
        default=900,
        ge=60,
        le=3600,
        description="bsk- token 有效時間（秒），預設 15 分鐘，最長 1 小時",
    )


class IssueBootstrapResponse(BaseModel):
    bootstrap_token: str = Field(
        ...,
        description="bsk- 開頭的單次使用 token；只在此回應出現一次",
    )
    expires_at: datetime
    agent_id: int
    agent_name: str
    endpoint_url: str = Field(
        ..., description="bootstrap 流程要 verify 的 endpoint_url；agent 端必須帶相同值"
    )


class BootstrapExchangeRequest(BaseModel):
    bootstrap_token: str = Field(..., description="admin 核發的 bsk- token")
    endpoint_url: str = Field(
        ...,
        description="agent 自身 endpoint_url，必須與 CSP 紀錄相符（防 token 在錯誤 agent 上被使用）",
    )
    label: str | None = Field(
        default=None,
        max_length=100,
        description="(可選) 多副本部署時用來標記這個 credential，例如 pod-1 / staging",
    )


class BootstrapExchangeResponse(BaseModel):
    service_token: str = Field(
        ..., description="csk- 開頭的長效 service token；agent 應寫進 state file"
    )
    credential_id: int
    issued_at: datetime
    label: str | None


class IssueStaticRequest(BaseModel):
    label: str | None = Field(default=None, max_length=100)


class CredentialResponse(BaseModel):
    id: int
    agent_id: int
    label: str | None
    is_active: bool
    is_legacy: bool
    issued_at: datetime
    rotated_at: datetime | None
    revoked_at: datetime | None
    has_previous_token: bool
    previous_expires_at: datetime | None
    client_cert_fingerprint: str | None


class RotateCredentialRequest(BaseModel):
    grace_seconds: int = Field(
        default=24 * 3600,
        ge=60,
        le=7 * 24 * 3600,
        description="輪替後，舊 token 仍可被驗證的 grace 視窗（秒）。預設 24h。",
    )


def _serialize_credential(cred: AgentCredential) -> CredentialResponse:
    return CredentialResponse(
        id=cred.id,
        agent_id=cred.agent_id,
        label=cred.label,
        is_active=cred.is_active,
        is_legacy=cred.is_legacy,
        issued_at=cred.service_token_issued_at,
        rotated_at=cred.service_token_rotated_at,
        revoked_at=cred.revoked_at,
        has_previous_token=bool(cred.service_token_previous_envelope),
        previous_expires_at=cred.service_token_previous_expires_at,
        client_cert_fingerprint=cred.client_cert_fingerprint,
    )


def _resolve_agent(db: Session, agent_id: int) -> Agent:
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    return agent


def _resolve_credential(
    db: Session, agent_id: int, credential_id: int
) -> AgentCredential:
    cred = (
        db.query(AgentCredential)
        .filter(
            AgentCredential.id == credential_id,
            AgentCredential.agent_id == agent_id,
        )
        .first()
    )
    if not cred:
        raise HTTPException(status_code=404, detail="Credential 不存在")
    return cred


# ---- Endpoints -------------------------------------------------------------


@router.post(
    "/{agent_id}/issue-bootstrap", response_model=IssueBootstrapResponse
)
def issue_bootstrap(
    agent_id: int,
    payload: IssueBootstrapRequest,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin: mint a single-use bsk- token for an agent.

    The plaintext is returned exactly once; CSP only stores its sha256
    hash. Re-issuing while a previous bootstrap is still pending
    invalidates the previous token.
    """
    from datetime import timedelta as _td

    agent = _resolve_agent(db, agent_id)
    plaintext = agent_credential_service.issue_bootstrap_token(
        db,
        agent=agent,
        issuer=admin,
        ttl=_td(seconds=payload.ttl_seconds),
    )
    db.commit()
    db.refresh(agent)
    return IssueBootstrapResponse(
        bootstrap_token=plaintext,
        expires_at=agent.bootstrap_token_expires_at,
        agent_id=agent.id,
        agent_name=agent.name,
        endpoint_url=agent.endpoint_url,
    )


@router.post(
    "/{agent_id}/bootstrap", response_model=BootstrapExchangeResponse
)
def bootstrap_exchange(
    agent_id: int,
    payload: BootstrapExchangeRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Public (token-gated): exchange a bsk- for a long-lived csk-.

    Anyone holding a valid bsk- can call this — the bsk- itself is the
    auth. ``endpoint_url`` must match the agent's registered URL to
    stop a leaked token from being replayed against a different agent.
    """
    agent = _resolve_agent(db, agent_id)
    try:
        cred, plaintext = agent_credential_service.consume_bootstrap_token(
            db,
            agent=agent,
            presented_token=payload.bootstrap_token,
            presented_endpoint_url=payload.endpoint_url,
            label=payload.label,
        )
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    db.commit()
    db.refresh(cred)
    invalidate_agent_token_cache(agent_id)
    return BootstrapExchangeResponse(
        service_token=plaintext,
        credential_id=cred.id,
        issued_at=cred.service_token_issued_at,
        label=cred.label,
    )


@router.post(
    "/{agent_id}/credentials/issue-static",
    response_model=BootstrapExchangeResponse,
)
def issue_static_credential(
    agent_id: int,
    payload: IssueStaticRequest,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Phase F (Tier 0): admin direct-issues a credential without bootstrap.

    For agents that cannot run the bootstrap CLI — third-party,
    non-Python, or rapid cutover from the old fleet-shared env var.
    No automatic rotation; admin must rotate periodically.
    """
    agent = _resolve_agent(db, agent_id)
    cred, plaintext = agent_credential_service.issue_static_credential(
        db,
        agent=agent,
        issuer=admin,
        label=payload.label,
    )
    db.commit()
    db.refresh(cred)
    invalidate_agent_token_cache(agent_id)
    return BootstrapExchangeResponse(
        service_token=plaintext,
        credential_id=cred.id,
        issued_at=cred.service_token_issued_at,
        label=cred.label,
    )


@router.get("/{agent_id}/credentials", response_model=list[CredentialResponse])
def list_credentials(
    agent_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin: list all credentials (active + revoked) for an agent."""
    _resolve_agent(db, agent_id)
    rows = (
        db.query(AgentCredential)
        .filter(AgentCredential.agent_id == agent_id)
        .order_by(AgentCredential.service_token_issued_at.desc())
        .all()
    )
    return [_serialize_credential(r) for r in rows]


@router.post(
    "/{agent_id}/credentials/{credential_id}/rotate",
    response_model=BootstrapExchangeResponse,
)
def rotate_credential(
    agent_id: int,
    credential_id: int,
    payload: RotateCredentialRequest,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin: rotate one credential. Returns the new plaintext (one-shot).

    Old token stays valid for ``grace_seconds`` afterwards via
    ``service_token_previous_*`` so streaming SSE doesn't drop.
    """
    from datetime import timedelta as _td

    cred = _resolve_credential(db, agent_id, credential_id)
    if not cred.is_active:
        raise HTTPException(status_code=400, detail="無法輪替已撤銷的 credential")
    plaintext = agent_credential_service.rotate_agent_credential(
        db,
        credential=cred,
        actor=admin,
        grace=_td(seconds=payload.grace_seconds),
    )
    db.commit()
    db.refresh(cred)
    invalidate_agent_token_cache(agent_id)
    return BootstrapExchangeResponse(
        service_token=plaintext,
        credential_id=cred.id,
        issued_at=cred.service_token_rotated_at or cred.service_token_issued_at,
        label=cred.label,
    )


@router.delete("/{agent_id}/credentials/{credential_id}")
def revoke_credential(
    agent_id: int,
    credential_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin: revoke a credential immediately (no grace window)."""
    cred = _resolve_credential(db, agent_id, credential_id)
    agent_credential_service.revoke_agent_credential(
        db,
        credential=cred,
        actor=admin,
        reason=f"manual revoke via /api/agents/{agent_id}/credentials/{credential_id}",
    )
    db.commit()
    invalidate_agent_token_cache(agent_id)
    return {"message": f"已撤銷 credential id={credential_id}"}


@router.get("/{agent_id}/credentials/me", response_model=CredentialResponse)
def get_my_credential(
    agent_id: int,
    db: Session = Depends(get_db),
    identity: agent_credential_service.CallerIdentity | None = Depends(verify_service_token),
):
    """Phase F (Tier 1): agent self-introspection.

    Authenticates with the agent's own service token; returns the
    matching credential row's metadata. Used by polling-style agents
    that don't run anila-core middleware to detect when their token
    was rotated by admin (so they can fetch the new one out-of-band).
    The plaintext token itself is NOT returned — agents must already
    hold it.
    """
    if identity is None or identity.kind != "agent" or identity.agent_id != agent_id:
        raise HTTPException(
            status_code=403,
            detail="此 endpoint 只能由 agent 自身的 service token 呼叫",
        )
    cred = _resolve_credential(db, agent_id, identity.credential_id)
    return _serialize_credential(cred)


@router.get("/me/runtime-config")
def get_my_runtime_config(
    db: Session = Depends(get_db),
    identity: agent_credential_service.CallerIdentity | None = Depends(verify_service_token),
):
    """Sprint 13 PR A3 — agent self-fetch of its admin-set runtime knobs.

    Authenticates with the agent's own ``X-CSP-Service-Token``; returns
    its current ``runtime_config``. The agent process polls this every
    30 s (Sprint 13 PR A4) so admin changes apply without a restart.

    Returns ``{"agent_id": int, "agent_name": str,
    "runtime_config": dict | None, "etag": str}``. The ETag is a stable
    hash of the JSON so the agent can short-circuit re-applying when
    the config hasn't changed since last poll.
    """
    if identity is None or identity.kind != "agent" or identity.agent_id is None:
        raise HTTPException(
            status_code=403,
            detail="此 endpoint 只能由 agent 自身的 service token 呼叫",
        )
    agent = db.query(Agent).filter(Agent.id == identity.agent_id).first()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent 不存在")

    import hashlib
    import json as _json
    cfg = agent.runtime_config
    serialized = _json.dumps(cfg, sort_keys=True, separators=(",", ":"))
    etag = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]

    return {
        "agent_id": agent.id,
        "agent_name": agent.name,
        "runtime_config": cfg,
        "etag": etag,
    }
