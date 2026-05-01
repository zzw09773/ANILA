"""Admin API for ``service_clients`` (Router / worker / admin tool tokens).

Sprint 8 X / Phase A. Sister to ``agents.py``'s credential endpoints —
same shape, different table. Only admins ever interact with these
rows; the clients themselves don't have a ``/credentials/me`` self
endpoint here (Router boots from a state file populated by the same
``anila-core agent bootstrap`` CLI used for AgenticRAG agents, but
the bootstrap target is this table instead of ``agent_credentials``).

Endpoint surface
================

  GET  /api/service-clients                         list active clients
  POST /api/service-clients                         create a client (admin)
  POST /api/service-clients/{id}/issue-static       admin → fresh csk-
  POST /api/service-clients/{id}/rotate             admin → rotate token
  DELETE /api/service-clients/{id}                  admin → revoke

We don't expose a "list credentials" endpoint here because each
``service_client`` row IS the credential — there's no 1:N relationship
like there is for agents. Multiple replicas of the same client share
one row and one token.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.service_client import ServiceClient
from app.models.user import User
from app.services import agent_credential_service
from app.services.audit_service import log_audit_event
from app.services.auth_service import require_admin
from app.services.service_token_envelope import (
    compute_lookup_hash,
    encode_service_token_envelope,
    generate_service_token,
)

router = APIRouter(prefix="/api/service-clients", tags=["Service Clients"])


# ---- Schemas ---------------------------------------------------------------


_CLIENT_TYPES = {"router", "worker", "admin_tool"}


class ServiceClientResponse(BaseModel):
    id: int
    client_name: str
    client_type: str
    description: str | None
    is_active: bool
    is_legacy: bool
    issued_at: datetime
    rotated_at: datetime | None
    revoked_at: datetime | None
    has_previous_token: bool
    previous_expires_at: datetime | None
    client_cert_fingerprint: str | None


class CreateServiceClientRequest(BaseModel):
    client_name: str = Field(..., min_length=1, max_length=100)
    client_type: str = Field(..., description="router | worker | admin_tool")
    description: str | None = Field(default=None, max_length=500)


class CreateServiceClientResponse(BaseModel):
    service_token: str
    client: ServiceClientResponse


class IssueStaticRequest(BaseModel):
    """No body fields; placeholder for symmetry with agent endpoint."""

    pass


class RotateClientRequest(BaseModel):
    grace_seconds: int = Field(
        default=24 * 3600, ge=60, le=7 * 24 * 3600
    )


def _serialize_client(client: ServiceClient) -> ServiceClientResponse:
    return ServiceClientResponse(
        id=client.id,
        client_name=client.client_name,
        client_type=client.client_type,
        description=client.description,
        is_active=client.is_active,
        is_legacy=client.is_legacy,
        issued_at=client.service_token_issued_at,
        rotated_at=client.service_token_rotated_at,
        revoked_at=client.revoked_at,
        has_previous_token=bool(client.service_token_previous_envelope),
        previous_expires_at=client.service_token_previous_expires_at,
        client_cert_fingerprint=client.client_cert_fingerprint,
    )


def _resolve_client(db: Session, client_id: int) -> ServiceClient:
    client = db.query(ServiceClient).filter(ServiceClient.id == client_id).first()
    if not client:
        raise HTTPException(status_code=404, detail="Service client 不存在")
    return client


def _client_ip(request: Request | None) -> Optional[str]:
    if request is None:
        return None
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return getattr(request.client, "host", None) if request.client else None


# ---- Endpoints -------------------------------------------------------------


@router.get("", response_model=list[ServiceClientResponse])
def list_clients(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(ServiceClient)
        .order_by(ServiceClient.created_at.desc())
        .all()
    )
    return [_serialize_client(c) for c in rows]


@router.post("", response_model=CreateServiceClientResponse)
def create_client(
    payload: CreateServiceClientRequest,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin creates a new service_client row + mints its initial token."""
    if payload.client_type not in _CLIENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"client_type 必須為 {sorted(_CLIENT_TYPES)} 之一",
        )
    existing = (
        db.query(ServiceClient)
        .filter(ServiceClient.client_name == payload.client_name)
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"client_name '{payload.client_name}' 已存在",
        )
    plaintext = generate_service_token()
    client = ServiceClient(
        client_name=payload.client_name,
        client_type=payload.client_type,
        description=payload.description,
        service_token_envelope=encode_service_token_envelope(plaintext),
        service_token_lookup_hash=compute_lookup_hash(plaintext),
        is_legacy=False,
        is_active=True,
    )
    db.add(client)
    db.flush()
    log_audit_event(
        db,
        actor=admin,
        action=agent_credential_service.AUDIT_TOKEN_ISSUED,
        resource_type="service_client",
        resource_id=client.id,
        detail=f"created service_client '{client.client_name}' ({client.client_type})",
        ip_address=_client_ip(request),
        metadata={"client_name": client.client_name, "client_type": client.client_type},
    )
    db.commit()
    db.refresh(client)
    return CreateServiceClientResponse(
        service_token=plaintext,
        client=_serialize_client(client),
    )


@router.post("/{client_id}/issue-static", response_model=CreateServiceClientResponse)
def issue_static_for_client(
    client_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin: replace an existing client's token with a freshly minted one.

    Unlike rotate, this does NOT keep the old token valid in a grace
    window — useful when a token leak is suspected and you want
    immediate cutoff. Otherwise prefer ``/rotate`` for production use.
    """
    client = _resolve_client(db, client_id)
    plaintext = generate_service_token()
    client.service_token_envelope = encode_service_token_envelope(plaintext)
    client.service_token_lookup_hash = compute_lookup_hash(plaintext)
    client.service_token_previous_envelope = None
    client.service_token_previous_lookup_hash = None
    client.service_token_previous_expires_at = None
    client.service_token_rotated_at = datetime.utcnow()
    client.is_legacy = False
    db.flush()
    log_audit_event(
        db,
        actor=admin,
        action=agent_credential_service.AUDIT_TOKEN_ISSUED,
        resource_type="service_client",
        resource_id=client.id,
        detail="static reissue (no grace window) by admin",
        ip_address=_client_ip(request),
    )
    db.commit()
    db.refresh(client)
    return CreateServiceClientResponse(
        service_token=plaintext,
        client=_serialize_client(client),
    )


@router.post("/{client_id}/rotate", response_model=CreateServiceClientResponse)
def rotate_client(
    client_id: int,
    payload: RotateClientRequest,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    client = _resolve_client(db, client_id)
    if not client.is_active:
        raise HTTPException(status_code=400, detail="無法輪替已撤銷的 service_client")
    plaintext = agent_credential_service.rotate_service_client(
        db,
        client=client,
        actor=admin,
        grace=timedelta(seconds=payload.grace_seconds),
    )
    db.commit()
    db.refresh(client)
    return CreateServiceClientResponse(
        service_token=plaintext,
        client=_serialize_client(client),
    )


@router.delete("/{client_id}")
def revoke_client(
    client_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    client = _resolve_client(db, client_id)
    agent_credential_service.revoke_service_client(
        db,
        client=client,
        actor=admin,
        reason=f"manual revoke via /api/service-clients/{client_id}",
    )
    db.commit()
    return {"message": f"已撤銷 service_client id={client_id}"}
