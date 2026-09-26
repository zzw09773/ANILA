"""Admin API for ``service_clients`` (Router / worker / admin tool tokens).

Sprint 8 X / Phase A. Only admins ever interact with these rows; the
clients themselves have no ``/credentials/me`` self endpoint here.

⚠ This table is NOT the agent path and must not be confused with it.
``agent_credentials`` issuance was retired to 410 on 2026-08-02 —
agents authenticate with a per-dispatch 5-minute RS256 JWT and hold no
long-lived secret. The ``anila-core agent bootstrap`` CLI this
docstring used to point at is gone with it. Platform service-to-service
identity (Router / worker / admin tool) still lives here. Configured
internal clients (at least ``router-primary``) are issued and rotated
by CSP onto a shared credential directory; these admin endpoints remain
for emergency revoke and manual rotate. Humans do not have to copy a
token. ``router-primary`` is live and depends on the file CSP writes.

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

import logging
from datetime import datetime, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.service_client import ServiceClient
from app.models.user import User
from app.services import agent_credential_service
from app.services.audit_service import log_audit_event
from app.services.auth_service import require_admin
from app.utils.client_ip import client_ip as _client_ip
from app.services.service_token_envelope import (
    compute_lookup_hash,
    encode_service_token_envelope,
    generate_service_token,
)
from app.schemas.base import ApiResponseModel
from app.services.internal_service_clients import (
    ProvisionOutcome,
    credential_is_file_provisioned,
    note_provision_outcomes,
    sync_configured_client_token,
)

router = APIRouter(prefix="/api/service-clients", tags=["Service Clients"])
logger = logging.getLogger(__name__)


# ---- Schemas ---------------------------------------------------------------


_CLIENT_TYPES = {"router", "worker", "admin_tool", "studio"}


class ServiceClientResponse(ApiResponseModel):
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
    client_type: str = Field(..., description="router | worker | admin_tool | studio")
    description: str | None = Field(default=None, max_length=500)


class CreateServiceClientResponse(BaseModel):
    """``delivery`` says where the plaintext went.

    ``file`` — internal client provisioned onto the credential file. The
    plaintext is not in this body.
    ``response`` — client has no credential file, so the plaintext is
    returned once.
    ``emergency_only`` — explicit re-issue. The plaintext is shown once
    and must not be stored.
    """

    service_token: str | None = None
    delivery: Literal["file", "response", "emergency_only"]
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


def _sync_token_file(db: Session, client: ServiceClient, *, required: bool) -> None:
    """Publish the committed row.

    ``required`` is create and rotate of a file-delivered client. A
    failed sync is ``delivery_error`` and readiness goes degraded.
    Emergency re-issue stays best-effort so the one-time plaintext is
    still returned; a failed sync on that path still degrades readiness.
    """
    name = client.client_name
    try:
        sync_configured_client_token(db, client)
    except Exception as exc:
        logger.error(
            "failed to sync credential file for service client %s: %s",
            name,
            exc.__class__.__name__,
        )
        note_provision_outcomes([ProvisionOutcome(name, "error")])
        if required:
            raise HTTPException(
                status_code=503,
                detail={
                    "delivery": "delivery_error",
                    "message": (
                        "credential committed but the token file was not published"
                    ),
                },
            ) from None


def _token_response(
    client: ServiceClient,
    plaintext: str,
    *,
    emergency: bool = False,
) -> CreateServiceClientResponse:
    serialized = _serialize_client(client)
    if emergency:
        return CreateServiceClientResponse(
            service_token=plaintext,
            delivery="emergency_only",
            client=serialized,
        )
    if credential_is_file_provisioned(client.client_name):
        return CreateServiceClientResponse(
            service_token=None,
            delivery="file",
            client=serialized,
        )
    return CreateServiceClientResponse(
        service_token=plaintext,
        delivery="response",
        client=serialized,
    )


def _resolve_client(db: Session, client_id: int) -> ServiceClient:
    client = db.query(ServiceClient).filter(ServiceClient.id == client_id).first()
    if not client:
        raise HTTPException(status_code=404, detail="Service client 不存在")
    return client


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
    _sync_token_file(db, client, required=True)
    return _token_response(client, plaintext)


@router.post("/{client_id}/issue-static", response_model=CreateServiceClientResponse)
def issue_static_for_client(
    client_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Emergency re-issue. The only path that returns a file-provisioned token.

    Unlike rotate, this does NOT keep the old token valid. The plaintext
    is labelled ``delivery=emergency_only`` and is shown once. Routine
    create and rotate of a file-provisioned client omit it.
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
    _sync_token_file(db, client, required=False)
    return _token_response(client, plaintext, emergency=True)


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
    _sync_token_file(db, client, required=True)
    return _token_response(client, plaintext)


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
    _sync_token_file(db, client, required=False)
    return {"message": f"已撤銷 service_client id={client_id}"}
