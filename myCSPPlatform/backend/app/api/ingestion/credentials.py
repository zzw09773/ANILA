"""``/api/ingestion/users/me/llm-credentials`` CRUD.

Sprint 4 refactor: was scoped to agents (``/agents/{agent_id}/...``);
now scoped to the calling user. Devs register their judge / external
LLM endpoints once, reusable across every collection they own. The
API key is encrypted at rest with AES-256-GCM via
``credential_crypto`` so audit logs / DB dumps never carry plaintext.

GET  list           returns rows WITHOUT the encrypted key bytes.
POST create         takes plaintext key, encrypts immediately,
                    returns row sans key.
PATCH update        ``endpoint_url`` / ``model_name`` only;
                    rotating the key requires DELETE + POST so the
                    audit trail is explicit.
DELETE              hard delete (rotation kill-switch).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.ingestion import UserLlmCredential
from app.models.user import User
from app.services.audit_service import log_audit_event
from app.services.auth_service import get_current_user
from app.services.credential_crypto import encrypt_credential


router = APIRouter(tags=["Ingestion / LLM credentials"])


class CredentialCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    endpoint_url: str = Field(..., min_length=1, max_length=1000)
    model_name: str = Field(..., min_length=1, max_length=200)
    api_key: str = Field(..., min_length=1, max_length=4000)


class CredentialUpdate(BaseModel):
    """Partial update — never includes ``api_key`` (delete+create instead)."""

    endpoint_url: str | None = Field(default=None, min_length=1, max_length=1000)
    model_name: str | None = Field(default=None, min_length=1, max_length=200)


class CredentialResponse(BaseModel):
    """Public projection — never carries the key bytes."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    created_by: int
    name: str
    endpoint_url: str
    model_name: str
    last_used_at: datetime | None
    created_at: datetime


def _own_credential(
    db: Session, user: User, credential_id: int
) -> UserLlmCredential:
    """Lookup with owner check. Admin can see anyone's; users see own only."""
    cred = (
        db.query(UserLlmCredential)
        .filter(UserLlmCredential.id == credential_id)
        .first()
    )
    if cred is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    if user.role != "admin" and cred.created_by != user.id:
        raise HTTPException(
            status_code=403, detail=f"No access to credential {credential_id}"
        )
    return cred


@router.post(
    "/api/ingestion/users/me/llm-credentials",
    response_model=CredentialResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_credential(
    payload: CredentialCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CredentialResponse:
    ciphertext, nonce, tag = encrypt_credential(payload.api_key)
    cred = UserLlmCredential(
        created_by=current_user.id,
        name=payload.name,
        endpoint_url=payload.endpoint_url,
        model_name=payload.model_name,
        api_key_encrypted=ciphertext,
        api_key_nonce=nonce,
        api_key_tag=tag,
    )
    db.add(cred)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Credential name '{payload.name}' already exists for you",
        ) from e
    db.refresh(cred)

    log_audit_event(
        db,
        actor=current_user,
        action="user_llm_credential_create",
        resource_type="user_llm_credential",
        resource_id=cred.id,
        metadata={
            "name": payload.name,
            "endpoint_url": payload.endpoint_url,
            "model_name": payload.model_name,
        },
    )
    return CredentialResponse.model_validate(cred)


@router.get(
    "/api/ingestion/users/me/llm-credentials",
    response_model=list[CredentialResponse],
)
def list_credentials(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[CredentialResponse]:
    rows = (
        db.query(UserLlmCredential)
        .filter(UserLlmCredential.created_by == current_user.id)
        .order_by(UserLlmCredential.id)
        .all()
    )
    return [CredentialResponse.model_validate(r) for r in rows]


@router.patch(
    "/api/ingestion/users/me/llm-credentials/{credential_id}",
    response_model=CredentialResponse,
)
def update_credential(
    credential_id: int,
    payload: CredentialUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CredentialResponse:
    cred = _own_credential(db, current_user, credential_id)
    changed = []
    if payload.endpoint_url is not None:
        cred.endpoint_url = payload.endpoint_url
        changed.append("endpoint_url")
    if payload.model_name is not None:
        cred.model_name = payload.model_name
        changed.append("model_name")
    if changed:
        db.commit()
        db.refresh(cred)
        log_audit_event(
            db,
            actor=current_user,
            action="user_llm_credential_update",
            resource_type="user_llm_credential",
            resource_id=cred.id,
            metadata={"changed": changed},
        )
    return CredentialResponse.model_validate(cred)


@router.delete(
    "/api/ingestion/users/me/llm-credentials/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_credential(
    credential_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Response:
    cred = _own_credential(db, current_user, credential_id)
    snapshot = {"name": cred.name, "endpoint_url": cred.endpoint_url}
    db.delete(cred)
    db.commit()
    log_audit_event(
        db,
        actor=current_user,
        action="user_llm_credential_delete",
        resource_type="user_llm_credential",
        resource_id=credential_id,
        metadata=snapshot,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
