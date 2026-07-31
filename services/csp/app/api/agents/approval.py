"""Agent approval workflow endpoints (approve / reject).

OE-1: admin may approve a registered (or disabled) agent with a single
click. No connection-test / trace-test / security-review gate. Reject
maps to ``disabled`` (the off terminal of the three-state set).
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.agent import Agent
from app.models.user import User
from app.schemas.contracts.agents import ApprovalStatus
from app.services.audit_service import log_audit_event
from app.services.auth_service import require_admin

from app.api.agents._common import _client_ip

router = APIRouter()

_APPROVABLE = frozenset(
    {
        ApprovalStatus.REGISTERED.value,
        ApprovalStatus.DISABLED.value,
    }
)


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
    if agent.approval_status == ApprovalStatus.APPROVED.value:
        return {"message": f"Agent「{agent.name}」已是核准狀態"}

    # OE-1: no trace-test / full_trace / review-gate requirement. Admin may
    # promote registered → approved, or re-enable disabled → approved.
    if agent.approval_status not in _APPROVABLE:
        detail = (
            f"Agent 目前狀態「{agent.approval_status}」無法核准"
            f"(僅 {' / '.join(sorted(_APPROVABLE))} 可核准)"
        )
        log_audit_event(
            db, actor=admin, action="approve", resource_type="agent",
            resource_id=agent.id, status="failure",
            detail=f"核准被拒:{detail}",
            ip_address=_client_ip(request), commit=True,
        )
        raise HTTPException(status_code=409, detail=detail)

    agent.approval_status = ApprovalStatus.APPROVED.value
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
    agent.approval_status = ApprovalStatus.DISABLED.value
    agent.approved_by = admin.id
    agent.approved_at = datetime.now(timezone.utc)
    db.commit()
    log_audit_event(
        db, actor=admin, action="reject", resource_type="agent",
        resource_id=agent.id, detail=f"停用 agent「{agent.name}」",
        ip_address=_client_ip(request), commit=True,
    )
    return {"message": f"已停用 agent「{agent.name}」"}
