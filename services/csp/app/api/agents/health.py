"""Agent endpoint probes (manual health-check + csk- test-connection).

Split verbatim from the former single-module ``app/api/agents.py``
(behavior-preserving refactor). D1 removed the on-demand Full Trace
diagnostic (``POST …/trace-test``); connection probe remains.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

import httpx
from anila_core.security import UnsafeEndpointError, validate_outbound_url

from app.database import get_db
from app.models.agent import Agent
from app.models.user import User
from app.services import agent_credential_service
from app.services.audit_service import log_audit_event
from app.services.auth_service import is_admin_tier, require_admin
from app.services.proxy.urls import join_upstream_path

from app.api.agents._common import (
    _client_ip,
    _require_developer_or_admin,
    _resolve_agent,
)

router = APIRouter()


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
    # Call-time SSRF guard — refuse to probe an endpoint that fails outbound
    # validation (TOCTOU / DNS-rebinding defense), even for an admin ping.
    # Guard once per host (scheme/hostname only; getaddrinfo is blocking),
    # then build the three probe URLs.
    try:
        validate_outbound_url(agent.endpoint_url, endpoint_kind="agent")
    except UnsafeEndpointError as exc:
        agent.health_status = "unhealthy"
        db.commit()
        log_audit_event(
            db, actor=admin, action="health_check",
            resource_type="agent", resource_id=agent.id,
            status="failure",
            detail=f"健康檢查拒絕: 端點未通過出向安全驗證 ({exc})",
            ip_address=ip,
            commit=True,
        )
        return {"status": "unhealthy", "detail": f"端點未通過出向安全驗證: {exc}"}
    probe_paths = ["/health", "/v1/models", "/"]
    probe_urls = [join_upstream_path(agent.endpoint_url, path) for path in probe_paths]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for path, url in zip(probe_paths, probe_urls):
                try:
                    resp = await client.get(url)
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


class TestConnectionResponse(BaseModel):
    reachable: bool
    # None = could not determine (endpoint unreachable).
    token_accepted: bool | None = None
    status_code: int | None = None
    detail: str


@router.post("/{agent_id}/test-connection", response_model=TestConnectionResponse)
async def test_agent_connection(
    agent_id: int,
    request: Request,
    current_user: User = Depends(_require_developer_or_admin),
    db: Session = Depends(get_db),
):
    """Probe the agent endpoint with its OWN csk- to confirm the operator wired
    ``CSP_SERVICE_TOKEN`` into the agent's .env (S-Q3). Owner-or-admin.

    Sends an empty ``messages`` body so the agent's inbound token check fires
    *before* any LLM work: 401 → the agent rejected our csk- (missing/wrong in
    .env); anything else (e.g. 400 "no user message") → token accepted, .env
    correctly wired. Connection error / timeout → unreachable.
    """
    agent = _resolve_agent(db, agent_id)
    if not is_admin_tier(current_user) and agent.owner_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="無權限測試此 Agent")

    # Call-time SSRF guard (TOCTOU / DNS-rebinding), same as health-check.
    # Guard the FINAL url that will actually be requested.
    url = join_upstream_path(agent.endpoint_url, "/v1/chat/completions")
    try:
        validate_outbound_url(url, endpoint_kind="agent")
    except UnsafeEndpointError as exc:
        raise HTTPException(status_code=400, detail=f"端點未通過出向安全驗證: {exc}")

    # The token the Router would present == whatever
    # get_active_plaintext_for_agent selects for outbound dispatch. Reuse it
    # so the probe tests the SAME credential CSP actually sends (consistent
    # ordering, incl. after a rotate of a non-latest credential — Nit#2).
    token = agent_credential_service.get_active_plaintext_for_agent(
        db, agent_id=agent.id
    )
    if not token:
        raise HTTPException(
            status_code=409,
            detail="此 Agent 尚無有效憑證,請先核發 csk- 再測試連線",
        )

    ip = _client_ip(request)
    body = {"model": agent.name, "messages": [], "stream": False}
    headers = {"X-CSP-Service-Token": token}
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(url, json=body, headers=headers)
        accepted = resp.status_code != 401
        detail = (
            "端點接受了該 csk-(agent .env 的 CSP_SERVICE_TOKEN 配對正確)"
            if accepted
            else "端點以 401 拒絕該 csk-(agent .env 未設或不符)"
        )
        log_audit_event(
            db, actor=current_user, action="test_connection",
            resource_type="agent", resource_id=agent.id,
            status="success" if accepted else "failure",
            detail=f"測試連線 → HTTP {resp.status_code}", ip_address=ip, commit=True,
        )
        return TestConnectionResponse(
            reachable=True, token_accepted=accepted,
            status_code=resp.status_code, detail=detail,
        )
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as exc:
        log_audit_event(
            db, actor=current_user, action="test_connection",
            resource_type="agent", resource_id=agent.id, status="failure",
            detail=f"測試連線無法連線: {exc}", ip_address=ip, commit=True,
        )
        return TestConnectionResponse(
            reachable=False, token_accepted=None,
            detail=f"無法連線到 agent 端點: {exc}",
        )
