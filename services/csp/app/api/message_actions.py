"""``/api/message-actions`` — OW-3 message-level custom actions.

docs/plans/ow3-message-actions-blueprint.md §4.

* Mutations: owner-only (``require_owner``).
* Management reads: admin-tier; non-owner ``body`` redacted as
  ``<owner-only>``.
* User surface: binding-scoped ``/visible`` + ``/invoke``.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.api.agents._common import _client_ip
from app.database import get_db
from app.models.user import User
from app.schemas.message_action import (
    ALLOWED_ACTION_ICONS,
    BindingOut,
    BindingsReplaceRequest,
    InvokeRequest,
    InvokeResponse,
    MessageActionAdminOut,
    MessageActionCreate,
    MessageActionOut,
    MessageActionUpdate,
)
from app.services import message_action_service as svc
from app.services.auth_service import get_current_user, require_admin, require_owner

router = APIRouter(prefix="/api/message-actions", tags=["訊息動作"])


@router.get("/icons")
def list_icons(_: User = Depends(require_admin)):
    return sorted(ALLOWED_ACTION_ICONS)


@router.get("/visible", response_model=list[MessageActionOut])
def list_visible_actions(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = svc.list_visible(db, user)
    return [
        MessageActionOut(
            id=r.id,
            name=r.name,
            label=r.label,
            icon=r.icon,
            kind=r.kind,
            result_mode=r.result_mode,
            choices=r.choices or [],
        )
        for r in rows
    ]


@router.get("/audit/export")
def export_audit(
    request: Request,
    since: Optional[datetime] = Query(None),
    until: Optional[datetime] = Query(None),
    limit: int = Query(5000, ge=1, le=50000),
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    rows = svc.export_audit(
        db,
        actor=owner,
        since=since,
        until=until,
        limit=limit,
        ip_address=_client_ip(request),
    )
    lines = [
        json.dumps(r, ensure_ascii=False, default=str) for r in rows
    ]
    body = "\n".join(lines) + ("\n" if lines else "")
    return Response(
        content=body,
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": (
                'attachment; filename="message-action-audit.ndjson"'
            ),
        },
    )


@router.get("", response_model=list[MessageActionAdminOut])
def list_actions(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows = svc.list_actions_admin(db)
    return [svc.serialize_admin(r, caller=admin) for r in rows]


@router.post(
    "",
    response_model=MessageActionAdminOut,
    status_code=status.HTTP_201_CREATED,
)
def create_action(
    payload: MessageActionCreate,
    request: Request,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    row = svc.create_action(
        db, payload=payload, actor=owner, ip_address=_client_ip(request)
    )
    return svc.serialize_admin(row, caller=owner)


@router.put("/{action_id}", response_model=MessageActionAdminOut)
def update_action(
    action_id: int,
    payload: MessageActionUpdate,
    request: Request,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    row = svc.update_action(
        db,
        action_id=action_id,
        payload=payload,
        actor=owner,
        ip_address=_client_ip(request),
    )
    return svc.serialize_admin(row, caller=owner)


@router.delete("/{action_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_action(
    action_id: int,
    request: Request,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    svc.delete_action(
        db,
        action_id=action_id,
        actor=owner,
        ip_address=_client_ip(request),
    )
    return None


@router.get("/{action_id}/bindings", response_model=list[BindingOut])
def get_bindings(
    action_id: int,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return svc.list_bindings(db, action_id)


@router.put("/{action_id}/bindings", response_model=list[BindingOut])
def put_bindings(
    action_id: int,
    payload: BindingsReplaceRequest,
    request: Request,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    return svc.replace_bindings(
        db,
        action_id=action_id,
        specs=payload.bindings,
        actor=owner,
        ip_address=_client_ip(request),
    )


@router.post("/{action_id}/invoke", response_model=InvokeResponse)
async def invoke(
    action_id: int,
    payload: InvokeRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    result = await svc.invoke_action(
        db,
        action_id=action_id,
        conversation_id=payload.conversation_id,
        message_id=payload.message_id,
        choice_id=payload.choice_id,
        user_input=payload.input,
        actor=user,
        ip_address=_client_ip(request),
    )
    return result
