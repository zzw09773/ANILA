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
from app.schemas.contracts.agents import ApprovalStatus
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
    if agent.approval_status == ApprovalStatus.APPROVED.value:
        return {"message": f"Agent「{agent.name}」已是核准狀態"}

    # doc 05 §6 approval blocker(hard block,無 grandfather):
    #   ① 未通過 Full Trace(trace_test_passed_at 為空)一律拒絕 —— 無 full
    #      trace 的 Agent 不能核准為正式可用。
    #   ② 狀態機:只有走完「連線測試 → trace-test」抵達 pending_security_review
    #      的 Agent 才可核准;其餘狀態(draft / pending_connection_test /
    #      pending_trace_test / rejected / disabled)一律 409。
    # 既有已 approved 的資料列維持 approved(上面 early-return),但一旦 unapprove
    # (如端點變更退回)就必須重跑 trace-test 才能再核准。
    if agent.trace_test_passed_at is None:
        detail = "此 Agent 尚未通過 Full Trace 測試(trace-test),不得核准為正式使用"
        log_audit_event(
            db, actor=admin, action="approve", resource_type="agent",
            resource_id=agent.id, status="failure",
            detail=f"核准被拒:{detail}(狀態={agent.approval_status})",
            ip_address=_client_ip(request), commit=True,
        )
        raise HTTPException(status_code=409, detail=detail)
    if agent.approval_status != ApprovalStatus.PENDING_SECURITY_REVIEW.value:
        detail = (
            f"Agent 目前狀態「{agent.approval_status}」不在可核准的安全審查關卡,"
            "無法核准"
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
