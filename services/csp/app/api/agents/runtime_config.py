"""Agent runtime-config endpoints (admin get/patch + agent self-fetch).

Split verbatim from the former single-module ``app/api/agents.py``
(behavior-preserving refactor).
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.agent import Agent
from app.models.user import User
from app.services import agent_credential_service
from app.services.audit_service import log_audit_event
from app.services.auth_service import is_admin_tier, verify_service_token

from app.api.agents._common import _client_ip, _require_developer_or_admin

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
    if not is_admin_tier(current_user) and agent.owner_user_id != current_user.id:
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
    if not is_admin_tier(current_user) and agent.owner_user_id != current_user.id:
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
