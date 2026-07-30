"""模型端點位址設定授權 API（P4.6b）。

指派／清單／撤銷僅 owner。``GET /me`` 供任何已登入者查自己是否可設定
端點位址（治理主控台解鎖表單用）——不經認證服務改動。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.endpoint_author_grant import EndpointAuthorGrant
from app.models.user import User
from app.services.auth_service import get_current_user, require_owner
from app.services.endpoint_author_service import (
    assign,
    can_set_endpoint_address,
    revoke,
)
from app.schemas.base import ApiResponseModel

router = APIRouter(prefix="/api/endpoint-authors", tags=["端點位址設定授權"])


class EndpointAuthorGrantRequest(BaseModel):
    user_id: int


class EndpointAuthorGrantResponse(ApiResponseModel):
    id: int
    user_id: int
    username: str | None = None
    granted_by: int | None
    granted_at: datetime
    revoked_at: datetime | None

    model_config = {"from_attributes": True}


class EndpointAuthorMeResponse(BaseModel):
    can_set_endpoint_address: bool


@router.get("/me", response_model=EndpointAuthorMeResponse)
def my_endpoint_author_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return EndpointAuthorMeResponse(
        can_set_endpoint_address=can_set_endpoint_address(db, current_user)
    )


@router.post("", response_model=EndpointAuthorGrantResponse, status_code=201)
def create_grant(
    body: EndpointAuthorGrantRequest,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == body.user_id).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=404, detail="使用者不存在")
    grant = assign(db, user=user, granted_by=owner)
    return EndpointAuthorGrantResponse(
        id=grant.id,
        user_id=grant.user_id,
        username=user.username,
        granted_by=grant.granted_by,
        granted_at=grant.granted_at,
        revoked_at=grant.revoked_at,
    )


@router.get("", response_model=list[EndpointAuthorGrantResponse])
def list_grants(
    include_revoked: int = Query(0, description="1 = 含歷史撤銷紀錄"),
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    query = db.query(EndpointAuthorGrant)
    if not include_revoked:
        query = query.filter(EndpointAuthorGrant.revoked_at.is_(None))
    grants = query.order_by(EndpointAuthorGrant.granted_at.desc()).all()
    user_ids = {g.user_id for g in grants}
    names = {}
    if user_ids:
        names = {
            u.id: u.username
            for u in db.query(User).filter(User.id.in_(user_ids)).all()
        }
    return [
        EndpointAuthorGrantResponse(
            id=g.id,
            user_id=g.user_id,
            username=names.get(g.user_id),
            granted_by=g.granted_by,
            granted_at=g.granted_at,
            revoked_at=g.revoked_at,
        )
        for g in grants
    ]


@router.delete("/{grant_id}")
def revoke_grant(
    grant_id: int,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    grant = (
        db.query(EndpointAuthorGrant)
        .filter(EndpointAuthorGrant.id == grant_id)
        .first()
    )
    if not grant:
        raise HTTPException(status_code=404, detail="指派不存在")
    revoke(db, grant=grant, actor=owner)
    return {
        "message": "已撤銷端點位址設定權限",
        "revoked_at": grant.revoked_at,
    }
