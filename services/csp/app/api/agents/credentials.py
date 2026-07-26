"""Per-agent credential endpoints + 密等鎖定(latch)開關。

Split verbatim from the former single-module ``app/api/agents.py``
(behavior-preserving refactor).

⚠ 命名警告(W1-3):``requires_encryption`` 這個欄位**不會加密任何東西**。
它的實際語意是「經過這個 agent 的對話一律單向鎖定密等」(one-way
classification latch)。平台沒有 at-rest 加密。欄位名與 audit action 為
相容性保留,但所有人看得到的字一律用「密等鎖定(latch)」。
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.agent import Agent
from app.models.agent_credential import AgentCredential
from app.models.user import User
from app.services import agent_credential_service
from app.services.audit_service import log_audit_event
from app.services.auth_service import (
    is_admin_tier,
    require_admin,
    verify_service_token,
)
from app.services.proxy_service import invalidate_agent_token_cache

from app.api.agents._common import (
    _client_ip,
    _require_developer_or_admin,
    _resolve_agent,
)

router = APIRouter()


class AgentEncryptionUpdate(BaseModel):
    requires_encryption: bool


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
    # W1-3:人看的字改成正確措辭 —— 這個開關從不加密任何東西,它只是把經過
    # 這個 agent 的對話**單向鎖定密等**(全 repo 零 at-rest 加密:
    # `pgcrypto|LUKS|dm-crypt|TDE` grep=0)。
    # `action="set_encryption"` 與欄位名 `requires_encryption` **刻意不動**:
    # 前者是既有稽核列的穩定識別字(改了歷史查詢就對不上),後者改名是 schema
    # 事務(掛 C5 legacy ledger 的退場條件)。
    log_audit_event(
        db, actor=admin, action="set_encryption", resource_type="agent",
        resource_id=agent.id,
        detail=(
            f"{'啟用' if payload.requires_encryption else '停用'} "
            f"agent「{agent.name}」密等鎖定(latch)模式"
        ),
        ip_address=_client_ip(http_request), commit=True,
    )
    return {
        "message": f"已更新 agent「{agent.name}」的密等鎖定(latch)設定",
        "requires_encryption": agent.requires_encryption,
    }


# ── Sprint 8 X / Phase A — service token bootstrap & credentials ─────────────
#
# Six new endpoints sit on top of the ``agent_credentials`` table:
#
#   POST /api/agents/{id}/issue-bootstrap        admin → bsk- token (one-shot)
#   POST /api/agents/{id}/bootstrap              caller → exchange bsk- for csk-
#   POST /api/agents/{id}/credentials/issue-static  admin (Phase F Tier 0)
#   GET  /api/agents/{id}/credentials            admin → list active credentials
#   POST /api/agents/{id}/credentials/{cid}/rotate  admin → rotate one credential
#   DELETE /api/agents/{id}/credentials/{cid}    admin → revoke one credential
#
# Plus one self-service endpoint for Tier 1 polling agents:
#
#   GET /api/agents/{id}/credentials/me          agent (auth = service token)


# ---- Schemas ---------------------------------------------------------------


class IssueBootstrapRequest(BaseModel):
    ttl_seconds: int = Field(
        default=900,
        ge=60,
        le=3600,
        description="bsk- token 有效時間（秒），預設 15 分鐘，最長 1 小時",
    )


class IssueBootstrapResponse(BaseModel):
    bootstrap_token: str = Field(
        ...,
        description="bsk- 開頭的單次使用 token；只在此回應出現一次",
    )
    expires_at: datetime
    agent_id: int
    agent_name: str
    endpoint_url: str = Field(
        ..., description="bootstrap 流程要 verify 的 endpoint_url；agent 端必須帶相同值"
    )


class BootstrapExchangeRequest(BaseModel):
    bootstrap_token: str = Field(..., description="admin 核發的 bsk- token")
    endpoint_url: str = Field(
        ...,
        description="agent 自身 endpoint_url，必須與 CSP 紀錄相符（防 token 在錯誤 agent 上被使用）",
    )
    label: str | None = Field(
        default=None,
        max_length=100,
        description="(可選) 多副本部署時用來標記這個 credential，例如 pod-1 / staging",
    )


class BootstrapExchangeResponse(BaseModel):
    service_token: str = Field(
        ..., description="csk- 開頭的長效 service token；agent 應寫進 state file"
    )
    credential_id: int
    issued_at: datetime
    label: str | None


class IssueStaticRequest(BaseModel):
    label: str | None = Field(default=None, max_length=100)


class CredentialResponse(BaseModel):
    id: int
    agent_id: int
    label: str | None
    is_active: bool
    is_legacy: bool
    issued_at: datetime
    rotated_at: datetime | None
    revoked_at: datetime | None
    has_previous_token: bool
    previous_expires_at: datetime | None
    client_cert_fingerprint: str | None


class RotateCredentialRequest(BaseModel):
    grace_seconds: int = Field(
        default=24 * 3600,
        ge=60,
        le=7 * 24 * 3600,
        description="輪替後，舊 token 仍可被驗證的 grace 視窗（秒）。預設 24h。",
    )


def _serialize_credential(cred: AgentCredential) -> CredentialResponse:
    return CredentialResponse(
        id=cred.id,
        agent_id=cred.agent_id,
        label=cred.label,
        is_active=cred.is_active,
        is_legacy=cred.is_legacy,
        issued_at=cred.service_token_issued_at,
        rotated_at=cred.service_token_rotated_at,
        revoked_at=cred.revoked_at,
        has_previous_token=bool(cred.service_token_previous_envelope),
        previous_expires_at=cred.service_token_previous_expires_at,
        client_cert_fingerprint=cred.client_cert_fingerprint,
    )


def _resolve_credential(
    db: Session, agent_id: int, credential_id: int
) -> AgentCredential:
    cred = (
        db.query(AgentCredential)
        .filter(
            AgentCredential.id == credential_id,
            AgentCredential.agent_id == agent_id,
        )
        .first()
    )
    if not cred:
        raise HTTPException(status_code=404, detail="Credential 不存在")
    return cred


# ---- Endpoints -------------------------------------------------------------


@router.post(
    "/{agent_id}/issue-bootstrap", response_model=IssueBootstrapResponse
)
def issue_bootstrap(
    agent_id: int,
    payload: IssueBootstrapRequest,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin: mint a single-use bsk- token for an agent.

    The plaintext is returned exactly once; CSP only stores its sha256
    hash. Re-issuing while a previous bootstrap is still pending
    invalidates the previous token.
    """
    from datetime import timedelta as _td

    agent = _resolve_agent(db, agent_id)
    plaintext = agent_credential_service.issue_bootstrap_token(
        db,
        agent=agent,
        issuer=admin,
        ttl=_td(seconds=payload.ttl_seconds),
    )
    db.commit()
    db.refresh(agent)
    return IssueBootstrapResponse(
        bootstrap_token=plaintext,
        expires_at=agent.bootstrap_token_expires_at,
        agent_id=agent.id,
        agent_name=agent.name,
        endpoint_url=agent.endpoint_url,
    )


@router.post(
    "/{agent_id}/bootstrap", response_model=BootstrapExchangeResponse
)
def bootstrap_exchange(
    agent_id: int,
    payload: BootstrapExchangeRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Public (token-gated): exchange a bsk- for a long-lived csk-.

    Anyone holding a valid bsk- can call this — the bsk- itself is the
    auth. ``endpoint_url`` must match the agent's registered URL to
    stop a leaked token from being replayed against a different agent.
    """
    agent = _resolve_agent(db, agent_id)
    try:
        cred, plaintext = agent_credential_service.consume_bootstrap_token(
            db,
            agent=agent,
            presented_token=payload.bootstrap_token,
            presented_endpoint_url=payload.endpoint_url,
            label=payload.label,
        )
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    db.commit()
    db.refresh(cred)
    invalidate_agent_token_cache(agent_id)
    return BootstrapExchangeResponse(
        service_token=plaintext,
        credential_id=cred.id,
        issued_at=cred.service_token_issued_at,
        label=cred.label,
    )


@router.post(
    "/{agent_id}/credentials/issue-static",
    response_model=BootstrapExchangeResponse,
)
def issue_static_credential(
    agent_id: int,
    payload: IssueStaticRequest,
    request: Request,
    current_user: User = Depends(_require_developer_or_admin),
    db: Session = Depends(get_db),
):
    """Owner (or admin) direct-issues a service token (``csk-``), no bootstrap.

    Skips the two-step ``bsk-`` → ``csk-`` bootstrap exchange: the agent
    owner mints one long-lived ``csk-`` directly and pastes it into the
    agent once — no bootstrap token to obtain, exchange, or swap out.

    The approval gate is unchanged: a ``pending`` agent still isn't routed
    until an admin approves it, so issuing a token early grants no routing.
    A non-admin may only issue for an agent they own. No automatic
    rotation; rotate periodically via the rotate endpoint.
    """
    agent = _resolve_agent(db, agent_id)
    if not is_admin_tier(current_user) and agent.owner_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="無權限為此 Agent 發行憑證")
    cred, plaintext = agent_credential_service.issue_static_credential(
        db,
        agent=agent,
        issuer=current_user,
        label=payload.label,
    )
    db.commit()
    db.refresh(cred)
    invalidate_agent_token_cache(agent_id)
    return BootstrapExchangeResponse(
        service_token=plaintext,
        credential_id=cred.id,
        issued_at=cred.service_token_issued_at,
        label=cred.label,
    )


@router.get("/{agent_id}/credentials", response_model=list[CredentialResponse])
def list_credentials(
    agent_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin: list all credentials (active + revoked) for an agent."""
    _resolve_agent(db, agent_id)
    rows = (
        db.query(AgentCredential)
        .filter(AgentCredential.agent_id == agent_id)
        .order_by(AgentCredential.service_token_issued_at.desc())
        .all()
    )
    return [_serialize_credential(r) for r in rows]


@router.post(
    "/{agent_id}/credentials/{credential_id}/rotate",
    response_model=BootstrapExchangeResponse,
)
def rotate_credential(
    agent_id: int,
    credential_id: int,
    payload: RotateCredentialRequest,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin: rotate one credential. Returns the new plaintext (one-shot).

    Old token stays valid for ``grace_seconds`` afterwards via
    ``service_token_previous_*`` so streaming SSE doesn't drop.
    """
    from datetime import timedelta as _td

    cred = _resolve_credential(db, agent_id, credential_id)
    if not cred.is_active:
        raise HTTPException(status_code=400, detail="無法輪替已撤銷的 credential")
    plaintext = agent_credential_service.rotate_agent_credential(
        db,
        credential=cred,
        actor=admin,
        grace=_td(seconds=payload.grace_seconds),
    )
    db.commit()
    db.refresh(cred)
    invalidate_agent_token_cache(agent_id)
    return BootstrapExchangeResponse(
        service_token=plaintext,
        credential_id=cred.id,
        issued_at=cred.service_token_rotated_at or cred.service_token_issued_at,
        label=cred.label,
    )


@router.delete("/{agent_id}/credentials/{credential_id}")
def revoke_credential(
    agent_id: int,
    credential_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin: revoke a credential immediately (no grace window)."""
    cred = _resolve_credential(db, agent_id, credential_id)
    agent_credential_service.revoke_agent_credential(
        db,
        credential=cred,
        actor=admin,
        reason=f"manual revoke via /api/agents/{agent_id}/credentials/{credential_id}",
    )
    db.commit()
    invalidate_agent_token_cache(agent_id)
    return {"message": f"已撤銷 credential id={credential_id}"}


@router.get("/{agent_id}/credentials/me", response_model=CredentialResponse)
def get_my_credential(
    agent_id: int,
    db: Session = Depends(get_db),
    identity: agent_credential_service.CallerIdentity | None = Depends(verify_service_token),
):
    """Phase F (Tier 1): agent self-introspection.

    Authenticates with the agent's own service token; returns the
    matching credential row's metadata. Used by polling-style agents
    that don't run anila-core middleware to detect when their token
    was rotated by admin (so they can fetch the new one out-of-band).
    The plaintext token itself is NOT returned — agents must already
    hold it.
    """
    if identity is None or identity.kind != "agent" or identity.agent_id != agent_id:
        raise HTTPException(
            status_code=403,
            detail="此 endpoint 只能由 agent 自身的 service token 呼叫",
        )
    cred = _resolve_credential(db, agent_id, identity.credential_id)
    return _serialize_credential(cred)
