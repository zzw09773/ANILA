"""Admin CRUD for ServiceAccessGrant.

    GET    /api/service-access-grants            list (with filters)
    POST   /api/service-access-grants            create grant (XOR validated)
    DELETE /api/service-access-grants/{grant_id} soft-revoke (set revoked_at)

End-user listing of "what services can I see" lives on
``GET /api/platform-links`` (already public to authenticated users; the
access filter is applied inside the platform_links router for non-admin
callers). Keeping that endpoint as the single source of truth avoids two
ways to ask the same question.

Soft-revoke (rather than DELETE) is deliberate — the row stays for audit,
the partial unique index ignores revoked rows, and ``granted_at`` history
remains queryable. A separate "really delete" path is intentionally not
provided; revoke is the only forward.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.department import Department
from app.models.platform_link import PlatformLink
from app.models.service_access_grant import ServiceAccessGrant
from app.models.user import User
from app.schemas.service_access_grant import (
    ServiceAccessGrantCreate,
    ServiceAccessGrantResponse,
)
from app.services.audit_service import log_audit_event
from app.services.auth_service import require_admin

router = APIRouter(tags=["服務存取權限"])


# ── Admin: list grants ──────────────────────────────────────────────────────
@router.get(
    "/api/service-access-grants",
    response_model=list[ServiceAccessGrantResponse],
)
def list_grants(
    user_id: int | None = Query(None, description="篩選特定 user 的 grants"),
    department_id: int | None = Query(None, description="篩選特定部門的 grants"),
    platform_link_id: int | None = Query(None, description="篩選特定服務的 grants"),
    include_revoked: bool = Query(False, description="是否包含已 revoke 的 grants"),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    query = db.query(ServiceAccessGrant)
    if user_id is not None:
        query = query.filter(ServiceAccessGrant.user_id == user_id)
    if department_id is not None:
        query = query.filter(ServiceAccessGrant.department_id == department_id)
    if platform_link_id is not None:
        query = query.filter(
            ServiceAccessGrant.platform_link_id == platform_link_id
        )
    if not include_revoked:
        query = query.filter(ServiceAccessGrant.revoked_at.is_(None))
    return query.order_by(ServiceAccessGrant.granted_at.desc()).all()


# ── Admin: create grant ─────────────────────────────────────────────────────
@router.post(
    "/api/service-access-grants",
    response_model=ServiceAccessGrantResponse,
    status_code=201,
)
def create_grant(
    request: ServiceAccessGrantCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    # Validate FK targets exist before INSERT — better 404 than the FK
    # error trickling out of the DB driver.
    link = (
        db.query(PlatformLink)
        .filter(PlatformLink.id == request.platform_link_id)
        .first()
    )
    if not link:
        raise HTTPException(status_code=404, detail="平台連結不存在")

    target_label: str
    if request.user_id is not None:
        target = db.query(User).filter(User.id == request.user_id).first()
        if not target:
            raise HTTPException(status_code=404, detail="使用者不存在")
        target_label = f"使用者「{target.username}」"
    else:
        dept = (
            db.query(Department)
            .filter(Department.id == request.department_id)
            .first()
        )
        if not dept:
            raise HTTPException(status_code=404, detail="部門不存在")
        target_label = f"部門「{dept.name}」"

    # Check for duplicate active grant — the partial unique index would
    # reject it but we want a useful 409 instead of the IntegrityError page.
    duplicate_q = db.query(ServiceAccessGrant).filter(
        ServiceAccessGrant.platform_link_id == request.platform_link_id,
        ServiceAccessGrant.revoked_at.is_(None),
    )
    if request.user_id is not None:
        duplicate_q = duplicate_q.filter(
            ServiceAccessGrant.user_id == request.user_id
        )
    else:
        duplicate_q = duplicate_q.filter(
            ServiceAccessGrant.department_id == request.department_id
        )
    if duplicate_q.first():
        raise HTTPException(
            status_code=409,
            detail=f"{target_label} 對「{link.name}」已有 active grant，"
            "請先 revoke 舊 grant 再 re-grant",
        )

    grant = ServiceAccessGrant(
        user_id=request.user_id,
        department_id=request.department_id,
        platform_link_id=request.platform_link_id,
        granted_by=admin.id,
    )
    db.add(grant)
    db.commit()
    db.refresh(grant)
    log_audit_event(
        db,
        actor=admin,
        action="grant",
        resource_type="service_access_grant",
        resource_id=grant.id,
        detail=f"授權 {target_label} 存取「{link.name}」",
        commit=True,
    )
    return grant


# ── Admin: revoke grant ─────────────────────────────────────────────────────
@router.delete("/api/service-access-grants/{grant_id}")
def revoke_grant(
    grant_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    grant = (
        db.query(ServiceAccessGrant)
        .filter(ServiceAccessGrant.id == grant_id)
        .first()
    )
    if not grant:
        raise HTTPException(status_code=404, detail="Grant 不存在")
    if grant.revoked_at is not None:
        # Idempotent: revoking an already-revoked grant is a no-op rather
        # than an error. Return the existing revoked_at so callers can tell.
        return {"message": "Grant 已 revoke", "revoked_at": grant.revoked_at}

    grant.revoked_at = datetime.now(timezone.utc)
    db.commit()
    log_audit_event(
        db,
        actor=admin,
        action="revoke",
        resource_type="service_access_grant",
        resource_id=grant.id,
        detail=f"撤銷 grant id={grant.id} (link_id={grant.platform_link_id})",
        commit=True,
    )
    return {"message": "Grant 已 revoke", "revoked_at": grant.revoked_at}
