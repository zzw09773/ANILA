"""Agent runtime-config endpoints (admin get/patch; agent self-fetch retired).

Split verbatim from the former single-module ``app/api/agents.py``
(behavior-preserving refactor), then Task-1 cleanup: the agent
``GET /me/runtime-config`` poll target is gone because nothing in
production ever called it.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.agent import Agent
from app.models.user import User

from app.api.agents._common import (
    _require_developer_or_admin,
    ensure_agent_view_access,
)

router = APIRouter()


class AgentRuntimeConfigUpdate(BaseModel):
    """PATCH payload for ``runtime_config``.

    Setting ``runtime_config`` to ``None`` clears the override (agent
    falls back to code defaults). An explicit empty dict ``{}`` means
    "admin set empty" — different semantics from ``None``.
    """

    runtime_config: dict | None = None


@router.get("/{agent_id}/runtime-config")
def get_agent_runtime_config(
    agent_id: int,
    current_user: User = Depends(_require_developer_or_admin),
    db: Session = Depends(get_db),
):
    """Read the agent's persisted runtime config (inspection only).

    Used by the CSP admin UI ``AgentRuntimeConfigView.vue`` as a
    read-only view of any historically stored JSON. The official agent
    template never polls this value; writes are 410.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    ensure_agent_view_access(agent, current_user)
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
    """Runtime-config writes are retired.

    CSP still stores a ``runtime_config`` column, but the official agent
    template never started the poller and the poll target itself is now
    removed. Accepting admin edits while nothing applies them was a
    silent no-op (FAKE-CONTROLS §8). Refuse writes; GET remains for
    read-only inspection.
    """
    raise HTTPException(
        status_code=410,
        detail=(
            "runtime_config 熱更新未出貨:官方 agent 不會輪詢此設定。"
            "請在 agent 程式碼調整工具權限/護欄,勿再經此 API 寫入。"
        ),
    )
