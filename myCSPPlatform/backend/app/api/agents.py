"""Agent management API (JWT-protected control plane).

Credential tier: JWT access token for all endpoints here.
Data plane list endpoint (GET /v1/agents, API Key auth) lives in proxy.py.
"""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.agent import Agent, UserAgentPermission
from app.models.user import User
from app.services.audit_service import log_audit_event
from app.services.auth_service import get_current_user, require_admin

router = APIRouter(prefix="/api/agents", tags=["Agent 管理"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class AgentRegisterRequest(BaseModel):
    name: str
    endpoint_url: str
    description_for_router: str
    api_version: str = "v1"
    base_model_id: int | None = None
    capabilities: dict | None = None
    input_schema: dict | None = None


class AgentResponse(BaseModel):
    id: int
    name: str
    owner_user_id: int
    endpoint_url: str
    api_version: str
    description_for_router: str
    health_status: str
    approval_status: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _require_developer_or_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role not in ("admin", "developer"):
        raise HTTPException(status_code=403, detail="需要開發者或管理員權限")
    return current_user


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/register", response_model=AgentResponse)
def register_agent(
    request: AgentRegisterRequest,
    current_user: User = Depends(_require_developer_or_admin),
    db: Session = Depends(get_db),
):
    existing = db.query(Agent).filter(Agent.name == request.name).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Agent 名稱「{request.name}」已存在")

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
        resource_id=agent.id, detail=f"註冊 agent「{agent.name}」", commit=True,
    )
    return agent


@router.get("", response_model=list[AgentResponse])
def list_agents(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.role == "admin":
        agents = db.query(Agent).order_by(Agent.created_at.desc()).all()
    else:
        agents = db.query(Agent).filter(Agent.owner_user_id == current_user.id).all()
    return agents


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
    return agent


@router.post("/{agent_id}/approve")
def approve_agent(
    agent_id: int,
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
        resource_id=agent.id, detail=f"核准 agent「{agent.name}」", commit=True,
    )
    return {"message": f"已核准 agent「{agent.name}」"}


@router.post("/{agent_id}/reject")
def reject_agent(
    agent_id: int,
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
        resource_id=agent.id, detail=f"拒絕 agent「{agent.name}」", commit=True,
    )
    return {"message": f"已拒絕 agent「{agent.name}」"}
