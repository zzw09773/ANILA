# -*- coding: utf-8 -*-
"""緊急輪替簽章金鑰。擁有者與管理員、CSRF、稽核。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.services.audit_service import log_audit_event_or_raise
from app.services.auth_service import require_admin
from app.services.jwt_keyring import EMERGENCY_CONFIRM_TEXT, emergency_rotate
from app.utils.client_ip import client_ip as _client_ip

router = APIRouter(prefix="/api/auth/jwt-keyring", tags=["auth"])


class EmergencyRotationBody(BaseModel):
    confirm: str = ""


@router.post("/emergency-rotation")
def emergency_rotation(
    body: EmergencyRotationBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> dict:
    if body.confirm != EMERGENCY_CONFIRM_TEXT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"確認文字不符。要繼續請送：{EMERGENCY_CONFIRM_TEXT}",
        )
    result = emergency_rotate(db)
    log_audit_event_or_raise(
        db,
        actor=current_user,
        action="jwt_signing_key_emergency_rotation",
        resource_type="jwt_signing_key",
        resource_id=result.kid,
        detail=EMERGENCY_CONFIRM_TEXT,
        ip_address=_client_ip(request),
        metadata={
            "new_kid": result.kid,
            "retired_kids": list(result.retired_kids),
        },
        commit=False,
    )
    db.commit()
    from app.services.token_revocation_publisher import publish_kid_revocations_sync

    publish_kid_revocations_sync(result.retired_kids, timeout=2.0)
    return {
        "kid": result.kid,
        "retired_kids": list(result.retired_kids),
        "detail": EMERGENCY_CONFIRM_TEXT,
    }
