"""Agent endpoint probes (manual health-check + dispatch-token test-connection).

Split verbatim from the former single-module ``app/api/agents.py``
(behavior-preserving refactor). D1 removed the on-demand Full Trace
diagnostic (``POST …/trace-test``); connection probe remains.

2026-07-30 false-green fix: connection test reports host / credentials /
path as distinct facts; health probe no longer treats a `/`-only hit as
``healthy``.

P2.1: test-connection probes with the same signed dispatch JWT that
outbound agent dispatch uses (requesting admin as probe identity).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

import httpx
from anila_core.security import UnsafeEndpointError, validate_outbound_url

from app.database import get_db
from app.models.agent import Agent
from app.models.user import User
from app.services.audit_service import log_audit_event
from app.services.auth_service import require_admin
from app.services.endpoint_author_service import can_see_endpoint_address
from app.services.health_checker import (
    HEALTH_DEGRADED,
    HEALTH_HEALTHY,
    HEALTH_UNHEALTHY,
    probe_model_health_detailed,
)
from app.services.proxy.headers import build_agent_headers
from app.services.proxy.urls import join_upstream_path

from app.api.agents._common import (
    _client_ip,
    _require_developer_or_admin,
    _resolve_agent,
    ensure_agent_view_access,
)

router = APIRouter()

# Chat-path responses that prove the versioned route exists *and* the
# presented dispatch credential got past inbound auth. Agent
# empty-messages → 400 is the canonical success; 422 is the
# structured-validation cousin. 401/403 are NOT success: gateways often
# authenticate before routing, so a wrong path returns 401 too. Other
# 4xx (405/429/…) only prove the path answered — credentials stay
# unknown so we do not over-claim.
_AUTH_CHALLENGE = frozenset({401, 403})
_PATH_MISSING = frozenset({404})
_CREDS_AND_PATH_OK = frozenset({400, 422}) | frozenset(range(200, 300))


def _classify_connection_status(status_code: int) -> tuple[bool, bool | None, bool | None, str]:
    """Map an HTTP status from ``POST …/v1/chat/completions`` to facts.

    Returns ``(host_reachable, credentials_accepted, path_verified, detail)``.
    """
    if status_code in _AUTH_CHALLENGE:
        return (
            True,
            None,
            None,
            (
                f"主機有回應（HTTP {status_code}）。"
                f"未驗證路徑：閘道常在路由前認証，錯誤路徑也可能回 {status_code}。"
                "未驗證憑證。"
            ),
        )
    if status_code in _PATH_MISSING:
        return (
            True,
            None,
            False,
            (
                f"主機有回應，但呼叫路徑不存在（HTTP {status_code}）。"
                "路徑未通過驗證；憑證是否被接受無法由此判斷。"
            ),
        )
    if status_code >= 500:
        return (
            True,
            None,
            True,
            (
                f"路徑有回應但上游錯誤（HTTP {status_code}）。"
                "已確認主機與路徑；憑證是否接受無法單憑此判斷。"
                "不視為連線驗證成功。"
            ),
        )
    if status_code in _CREDS_AND_PATH_OK:
        return (
            True,
            True,
            True,
            (
                f"路徑與憑證皆通過：端點接受了該派工 JWT 並處理請求"
                f"（HTTP {status_code}）。"
            ),
        )
    # Other 4xx (405, 415, 429, …): path answered, credentials unclear.
    return (
        True,
        None,
        True,
        (
            f"路徑有回應（HTTP {status_code}）。"
            "已確認路徑；憑證是否接受無法單憑此判斷。"
            "不視為連線驗證成功。"
        ),
    )


def _safe_unreachable_detail(
    exc: BaseException,
    *,
    db: Session,
    caller: User,
) -> str:
    """Connection-error detail: never leak an address-shaped string unless
    ``can_see_endpoint_address`` says the caller may see one. httpx errors
    commonly embed the request URL.
    """
    if can_see_endpoint_address(db, caller):
        return f"無法連線到 agent 端點: {exc}"
    return "無法連線到 agent 端點（主機未回應或逾時）"


def _safe_ssrf_detail(
    exc: BaseException,
    *,
    db: Session,
    caller: User,
) -> str:
    """SSRF-reject detail: ``UnsafeEndpointError`` often embeds hostname."""
    if can_see_endpoint_address(db, caller):
        return f"端點未通過出向安全驗證: {exc}"
    return "端點未通過出向安全驗證"


@router.post("/{agent_id}/health-check")
async def trigger_agent_health_check(
    agent_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Probe an agent's endpoint and update ``health_status``.

    Mirrors ``POST /api/models/{id}/health-check`` for parity on the
    management UI: the admin clicks "檢查", the backend tries platform-used
    liveness paths (and optionally `/` as a weak host signal), and the DB
    stamp is updated so the colored dot in the agents list reflects reality.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 不存在")

    ip = _client_ip(request)
    # Call-time SSRF guard — refuse to probe an endpoint that fails outbound
    # validation (TOCTOU / DNS-rebinding defense), even for an admin ping.
    # Guard once per host (scheme/hostname only; getaddrinfo is blocking).
    try:
        validate_outbound_url(agent.endpoint_url, endpoint_kind="agent")
    except UnsafeEndpointError as exc:
        agent.health_status = HEALTH_UNHEALTHY
        db.commit()
        # Audit detail must not embed hostname — serialize_audit_log does
        # not scrub ``detail`` for non-authors.
        log_audit_event(
            db, actor=admin, action="health_check",
            resource_type="agent", resource_id=agent.id,
            status="failure",
            detail="健康檢查拒絕: 端點未通過出向安全驗證",
            ip_address=ip,
            commit=True,
        )
        return {
            "status": HEALTH_UNHEALTHY,
            "detail": _safe_ssrf_detail(exc, db=db, caller=admin),
        }

    # Release pooled connection before the outbound probe (≤10s).
    # skip_validate: already guarded above — keep once-per-host.
    db.commit()
    status, latency_ms = await probe_model_health_detailed(
        agent.endpoint_url, endpoint_kind="agent", skip_validate=True
    )
    agent.health_status = status
    db.commit()

    if status == HEALTH_HEALTHY:
        detail = "真實探測路徑（/health 或 /v1/models）有回應"
        audit_status = "success"
    elif status == HEALTH_DEGRADED:
        detail = (
            "僅根路徑有回應，或探測逾時——"
            "主機可能存活，但未驗證平台實際使用的路徑"
        )
        audit_status = "success"
    else:
        detail = "無法連線到 agent 端點"
        audit_status = "failure"

    log_audit_event(
        db, actor=admin, action="health_check",
        resource_type="agent", resource_id=agent.id,
        status=audit_status,
        detail=f"手動健康檢查: {agent.name} → {status}",
        ip_address=ip,
        commit=True,
    )
    return {
        "status": status,
        "detail": detail,
        "latency_ms": latency_ms,
    }


class TestConnectionResponse(BaseModel):
    """Honest connection-test outcome — three facts, not one pass/fail.

    ``reachable`` / ``token_accepted`` remain as aliases so older clients
    keep working; ``token_accepted`` is no longer ``status != 401``.
    """

    host_reachable: bool
    credentials_accepted: bool | None = None
    path_verified: bool | None = None
    status_code: int | None = None
    detail: str

    # Aliases (populated from the three facts when omitted).
    reachable: bool | None = Field(default=None)
    token_accepted: bool | None = Field(default=None)

    @model_validator(mode="after")
    def _fill_aliases(self) -> TestConnectionResponse:
        if self.reachable is None:
            self.reachable = self.host_reachable
        if self.token_accepted is None:
            self.token_accepted = self.credentials_accepted
        return self


@router.post("/{agent_id}/test-connection", response_model=TestConnectionResponse)
async def test_agent_connection(
    agent_id: int,
    request: Request,
    current_user: User = Depends(_require_developer_or_admin),
    db: Session = Depends(get_db),
):
    """Probe the agent endpoint with a signed dispatch JWT (diagnostic only).

    Uses the same ``build_agent_headers`` path as live dispatch, with the
    requesting admin/developer as probe identity. Distinguishes host
    reachability, credential acceptance, and path verification. A 401 on
    the versioned chat path is NOT a pass: gateways often authenticate
    before routing, so a wrong path returns 401 too.
    """
    agent = _resolve_agent(db, agent_id)
    ensure_agent_view_access(agent, current_user)

    # Call-time SSRF guard (TOCTOU / DNS-rebinding), same as health-check.
    # Guard the FINAL url that will actually be requested.
    url = join_upstream_path(agent.endpoint_url, "/v1/chat/completions")
    try:
        validate_outbound_url(url, endpoint_kind="agent")
    except UnsafeEndpointError as exc:
        raise HTTPException(
            status_code=400,
            detail=_safe_ssrf_detail(exc, db=db, caller=current_user),
        )

    ip = _client_ip(request)
    body = {"model": agent.name, "messages": [], "stream": False}
    # Same builder as live CSP→agent dispatch; probe identity = requester.
    headers = build_agent_headers(
        user_id=current_user.id,
        department=current_user.department_id,
        agent_id=agent.id,
    )
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(url, json=body, headers=headers)
        host_ok, creds, path_ok, detail = _classify_connection_status(resp.status_code)
        verified = bool(creds and path_ok)
        log_audit_event(
            db, actor=current_user, action="test_connection",
            resource_type="agent", resource_id=agent.id,
            status="success" if verified else "failure",
            detail=f"測試連線 → HTTP {resp.status_code}", ip_address=ip, commit=True,
        )
        return TestConnectionResponse(
            host_reachable=host_ok,
            credentials_accepted=creds,
            path_verified=path_ok,
            status_code=resp.status_code,
            detail=detail,
        )
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as exc:
        log_audit_event(
            db, actor=current_user, action="test_connection",
            resource_type="agent", resource_id=agent.id, status="failure",
            detail="測試連線無法連線", ip_address=ip, commit=True,
        )
        return TestConnectionResponse(
            host_reachable=False,
            credentials_accepted=None,
            path_verified=None,
            detail=_safe_unreachable_detail(exc, db=db, caller=current_user),
        )
