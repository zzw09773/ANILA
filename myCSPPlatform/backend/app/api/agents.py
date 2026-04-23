"""Agent management API (JWT-protected control plane).

Credential tier: JWT access token for all endpoints here.
Data plane list endpoint (GET /v1/agents, API Key auth) lives in proxy.py.
"""
import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.agent import Agent, UserAgentPermission
from app.models.user import User
from app.services.audit_service import log_audit_event
from app.services.auth_service import get_current_user, require_admin

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
        "capabilities": agent.capabilities,
        "health_status": normalized,
        "approval_status": agent.approval_status,
        "requires_encryption": bool(getattr(agent, "requires_encryption", False)),
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
