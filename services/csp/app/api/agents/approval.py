"""Agent approval workflow endpoints (approve / reject).

Split verbatim from the former single-module ``app/api/agents.py``
(behavior-preserving refactor).
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.agent import Agent
from app.models.user import User
from app.services.audit_service import log_audit_event
from app.services.auth_service import require_admin

from app.api.agents._common import _client_ip

router = APIRouter()


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
