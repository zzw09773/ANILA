"""Shared helpers for the ``app.api.agents`` package.

Split verbatim from the former single-module ``app/api/agents.py``
(behavior-preserving refactor). Only helpers used by 2+ submodules
live here.
"""
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.models.agent import Agent
from app.models.user import User
from app.services.auth_service import get_current_user


def _require_developer_or_admin(current_user: User = Depends(get_current_user)) -> User:
    # Sprint owner-tier: owner 是 admin 之上的最高權限,凡 admin 能做的事 owner 也能做。
    # 漏掉 owner 會讓系統最高權限者反而拿不到開發者頁面 (download template、register
    # agent 等),這是 RBAC 新增層級時典型的回歸坑。和 auth_service._ADMIN_TIER_ROLES
    # 保持一致。
    if current_user.role not in ("admin", "developer", "owner"):
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


def _resolve_agent(db: Session, agent_id: int) -> Agent:
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    return agent
