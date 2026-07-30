"""模型端點位址設定授權服務（P4.6b）。

綁定表 ``endpoint_author_grants``；``users.role`` 不動。指派／撤銷僅
owner；可被指派者限 ``role=developer``。稽核寫入授與／撤銷雙方身分。
變更與稽核同一交易提交（batch-approve 先例：稽核失敗則整筆中止）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.endpoint_author_grant import EndpointAuthorGrant
from app.models.user import User
from app.services.audit_service import log_audit_event
from app.services.auth_service import is_owner


def get_active_grant(db: Session, user: User) -> EndpointAuthorGrant | None:
    return (
        db.query(EndpointAuthorGrant)
        .filter(
            EndpointAuthorGrant.user_id == user.id,
            EndpointAuthorGrant.revoked_at.is_(None),
        )
        .first()
    )


def can_set_endpoint_address(db: Session, user: User) -> bool:
    """True for the platform owner, or a currently designated developer."""
    if is_owner(user):
        return True
    if user.role != "developer":
        return False
    return get_active_grant(db, user) is not None


def require_endpoint_address_author(db: Session, user: User) -> User:
    """403 shaped like other authz gates when the caller may not set addresses."""
    if not can_set_endpoint_address(db, user):
        raise HTTPException(
            status_code=403,
            detail="需要端點位址設定權限",
        )
    return user


def assign(
    db: Session,
    *,
    user: User,
    granted_by: User,
) -> EndpointAuthorGrant:
    if not is_owner(granted_by):
        raise HTTPException(status_code=403, detail="需要 owner 權限")
    if user.role != "developer":
        raise HTTPException(
            status_code=400,
            detail="僅可指派開發者為端點位址設定者",
        )
    if not user.is_active:
        raise HTTPException(status_code=400, detail="使用者不存在或已停用")

    existing = get_active_grant(db, user)
    if existing:
        raise HTTPException(status_code=400, detail="已具有端點位址設定權限")

    grant = EndpointAuthorGrant(
        user_id=user.id,
        granted_by=granted_by.id,
    )
    db.add(grant)
    try:
        db.flush()
    except IntegrityError:
        # Concurrent grantors racing the partial unique index.
        db.rollback()
        raise HTTPException(status_code=400, detail="已具有端點位址設定權限")

    audit_row = log_audit_event(
        db,
        actor=granted_by,
        action="endpoint_author_grant",
        resource_type="user",
        resource_id=user.id,
        detail=(
            f"授予端點位址設定權限：由「{granted_by.username}」"
            f"授予「{user.username}」"
        ),
        commit=False,
    )
    if audit_row is None:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="稽核紀錄寫入失敗，端點位址設定授權未生效",
        )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="已具有端點位址設定權限")
    db.refresh(grant)
    return grant


def revoke(
    db: Session,
    *,
    grant: EndpointAuthorGrant,
    actor: User,
) -> EndpointAuthorGrant:
    if not is_owner(actor):
        raise HTTPException(status_code=403, detail="需要 owner 權限")
    if grant.revoked_at is not None:
        return grant

    grantee = db.query(User).filter(User.id == grant.user_id).first()
    grantee_name = grantee.username if grantee else f"user_id={grant.user_id}"

    grant.revoked_at = datetime.now(timezone.utc)
    audit_row = log_audit_event(
        db,
        actor=actor,
        action="endpoint_author_revoke",
        resource_type="user",
        resource_id=grant.user_id,
        detail=(
            f"撤銷端點位址設定權限：由「{actor.username}」"
            f"撤銷「{grantee_name}」（grant_id={grant.id}）"
        ),
        commit=False,
    )
    if audit_row is None:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="稽核紀錄寫入失敗，端點位址設定授權撤銷未生效",
        )
    db.commit()
    db.refresh(grant)
    return grant
