"""Per-agent credential endpoints + classification level setter.

P2.1 Task 2+4 (2026-08-02): agent-facing long-lived ``csk-`` / ``bsk-``
issuance and the Tier-1 ``GET .../credentials/me`` poll target are
retired (410). Dispatch identity is the 5-minute RS256 JWT; there is
no remaining agent-side consumer that needs an agent-kind credential
(runtime-config self-fetch is gone; ``GET /api/auth/revocations``
rejects ``kind=agent``).

Kept:
  * ``POST /{id}/classification`` — unrelated to service tokens
  * ``GET /{id}/credentials`` — admin inspection of any orphan rows
  * ``DELETE /{id}/credentials/{cid}`` — admin revoke of orphans

``service_clients`` issuance (``POST /api/service-clients/{id}/issue-static``)
is a different surface and is untouched. Shared envelope / verify
helpers in ``agent_credential_service`` remain for that path.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.agent_credential import AgentCredential
from app.models.user import User
from app.schemas.contracts.classification import ClassificationLevel
from app.services import agent_credential_service
from app.services.audit_service import log_audit_event
from app.services.auth_service import (
    get_current_user,
    is_admin_tier,
    require_admin,
)
from app.services.proxy_service import invalidate_agent_token_cache

from app.api.agents._common import (
    _client_ip,
    _require_agent_editor,
    _require_developer_or_admin,
    _resolve_agent,
    apply_default_classification_level,
    refuse_classification_downgrade,
)
from app.schemas.base import ApiResponseModel

router = APIRouter()

_AGENT_CSK_RETIRED_DETAIL = (
    "agent 長效 csk-/bsk- 憑證發行已廢止 (P2.1)。"
    "派工身分改走 5 分鐘 RS256 JWT＋JWKS；"
    "請見 docs/guides/developer-guide.md。"
)


class AgentClassificationUpdate(BaseModel):
    """Set the agent's default classification level (four-level vocabulary).

    Replaces the former boolean ``/encryption`` toggle. The legacy
    ``requires_encryption`` column is derived from the level using the
    conversation mirror threshold (``level >= 密``).
    """

    default_classification_level: ClassificationLevel


@router.post("/{agent_id}/classification")
def set_agent_classification(
    agent_id: int,
    payload: AgentClassificationUpdate,
    http_request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Owner / admin: set the agent's default classification level.

    Authorization matches ``PUT /api/agents/{id}`` (admin-tier or owner).
    Developers may raise the effective policy level but not lower it;
    administrator-tier callers may lower (legacy boolean floor clears with
    the derived write). Conversations answered by this agent latch via the
    existing proxy policy path.
    """
    agent = _resolve_agent(db, agent_id)
    _require_agent_editor(agent, current_user)
    new_level = payload.default_classification_level
    refuse_classification_downgrade(agent, new_level, current_user)
    effective_transition, changed, stored_transition = (
        apply_default_classification_level(
            agent,
            new_level,
            db=db,
            allow_downgrade=is_admin_tier(current_user),
        )
    )
    if not changed:
        return {
            "message": f"agent「{agent.name}」預設分類等級未變更",
            "default_classification_level": agent.default_classification_level,
            "requires_encryption": agent.requires_encryption,
        }
    # Audit whenever the *effective* policy level changed — including the
    # legacy-floor drop when stored value is unchanged but the derived
    # boolean clears. Describe the transition in effective terms.
    if effective_transition is not None:
        from_eff, to_eff = effective_transition
        from_stored, to_stored = stored_transition
        audit_row = log_audit_event(
            db,
            actor=current_user,
            action="set_classification",
            resource_type="agent",
            resource_id=agent.id,
            detail=(
                f"變更 agent「{agent.name}」有效分類等級："
                f"{from_eff.to_storage()} → {to_eff.to_storage()}"
            ),
            ip_address=_client_ip(http_request),
            metadata={
                "from_level": from_eff.to_storage(),
                "to_level": to_eff.to_storage(),
                "from_stored_level": from_stored.to_storage(),
                "to_stored_level": to_stored.to_storage(),
                "requires_controlled_access": agent.requires_encryption,
            },
            commit=False,
        )
        if audit_row is None:
            raise HTTPException(
                status_code=500,
                detail="稽核紀錄寫入失敗，分類等級變更未生效",
            )
    db.commit()
    db.refresh(agent)
    level_label = agent.default_classification_level
    return {
        "message": f"已更新 agent「{agent.name}」的預設分類等級為「{level_label}」",
        "default_classification_level": agent.default_classification_level,
        "requires_encryption": agent.requires_encryption,
    }


# ── Agent csk-/bsk- surface (retired P2.1 Task 2+4) ─────────────────────────
#
# Former endpoints (all now 410 except list/revoke):
#
#   POST /api/agents/{id}/issue-bootstrap
#   POST /api/agents/{id}/bootstrap
#   POST /api/agents/{id}/credentials/issue-static
#   POST /api/agents/{id}/credentials/{cid}/rotate
#   GET  /api/agents/{id}/credentials/me
#
# Admin cleanup still available:
#
#   GET    /api/agents/{id}/credentials
#   DELETE /api/agents/{id}/credentials/{cid}


# ---- Schemas (kept for OpenAPI / re-exports; unused by 410 stubs) ----------


class IssueBootstrapRequest(BaseModel):
    ttl_seconds: int = Field(
        default=900,
        ge=60,
        le=3600,
        description="bsk- token 有效時間（秒），預設 15 分鐘，最長 1 小時",
    )


class IssueBootstrapResponse(ApiResponseModel):
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


class BootstrapExchangeResponse(ApiResponseModel):
    service_token: str = Field(
        ..., description="csk- 開頭的長效 service token；agent 應寫進 state file"
    )
    credential_id: int
    issued_at: datetime
    label: str | None


class IssueStaticRequest(BaseModel):
    label: str | None = Field(default=None, max_length=100)


class CredentialResponse(ApiResponseModel):
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


def _raise_agent_csk_retired() -> None:
    raise HTTPException(status_code=410, detail=_AGENT_CSK_RETIRED_DETAIL)


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
    """Retired: admin no longer mints agent ``bsk-`` tokens."""
    _raise_agent_csk_retired()


@router.post(
    "/{agent_id}/bootstrap", response_model=BootstrapExchangeResponse
)
def bootstrap_exchange(
    agent_id: int,
    payload: BootstrapExchangeRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Retired: ``bsk-`` → ``csk-`` exchange is gone."""
    _raise_agent_csk_retired()


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
    """Retired: direct agent ``csk-`` minting is gone.

    Platform-internal ``POST /api/service-clients/{id}/issue-static`` is
    a different endpoint and still works.
    """
    _raise_agent_csk_retired()


@router.get("/{agent_id}/credentials", response_model=list[CredentialResponse])
def list_credentials(
    agent_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin: list all credentials (active + revoked) for an agent.

    Issuance is retired; this remains so an admin can confirm the table
    is empty (or revoke any unexpected orphan row).
    """
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
    """Retired: rotating an agent credential would mint a new ``csk-``."""
    _raise_agent_csk_retired()


@router.delete("/{agent_id}/credentials/{credential_id}")
def revoke_credential(
    agent_id: int,
    credential_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin: revoke a leftover credential immediately (no grace window)."""
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
def get_my_credential(agent_id: int):
    """Retired (Task 2): Tier-1 rotation-detection poll target.

    Premise check (2026-08-02): the only Q19 justifications for an
    agent-kind ``csk-`` were runtime-config self-fetch (removed) and a
    revocation list (never built agent-side; the live endpoint rejects
    ``kind=agent``). No in-tree caller. Retiring rather than scoping to
    bootstrap-tier agents — bootstrap itself is also 410.
    """
    _raise_agent_csk_retired()
