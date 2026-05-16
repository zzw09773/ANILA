from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.platform_link import PlatformLink
from app.models.user import User
from app.schemas.platform_link import (
    PlatformLinkCreate,
    PlatformLinkUpdate,
    PlatformLinkResponse,
)
from app.services.access_control import accessible_links_for
from app.services.audit_service import log_audit_event
from app.services.auth_service import get_current_user, is_admin_tier, require_admin

router = APIRouter(prefix="/api/platform-links", tags=["平台連結"])


@router.get("", response_model=list[PlatformLinkResponse])
def list_links(
    include_inactive: bool = False,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # admin / owner 都享全可視 + include_inactive 切換;
    # 一般 user 走 access_control 的 role gate + grant check,
    # include_inactive 對非 admin-tier 靜默忽略。
    if is_admin_tier(current_user):
        query = db.query(PlatformLink).order_by(
            PlatformLink.sort_order, PlatformLink.created_at
        )
        if not include_inactive:
            query = query.filter(PlatformLink.is_active == True)
        return query.all()
    return accessible_links_for(db, current_user)


@router.post("", response_model=PlatformLinkResponse)
def create_link(
    request: PlatformLinkCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    link = PlatformLink(**request.model_dump())
    db.add(link)
    db.commit()
    db.refresh(link)
    log_audit_event(
        db,
        actor=admin,
        action="create",
        resource_type="platform_link",
        resource_id=link.id,
        detail=f"建立平台連結「{link.name}」",
        commit=True,
    )
    return link


@router.put("/{link_id}", response_model=PlatformLinkResponse)
def update_link(
    link_id: int,
    request: PlatformLinkUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    link = db.query(PlatformLink).filter(PlatformLink.id == link_id).first()
    if not link:
        raise HTTPException(status_code=404, detail="連結不存在")

    update_data = request.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(link, field, value)

    db.commit()
    db.refresh(link)
    log_audit_event(
        db,
        actor=admin,
        action="update",
        resource_type="platform_link",
        resource_id=link.id,
        detail=f"更新平台連結「{link.name}」",
        commit=True,
    )
    return link


@router.delete("/{link_id}")
def delete_link(
    link_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    link = db.query(PlatformLink).filter(PlatformLink.id == link_id).first()
    if not link:
        raise HTTPException(status_code=404, detail="連結不存在")
    link.is_active = False
    db.commit()
    log_audit_event(
        db,
        actor=admin,
        action="deactivate",
        resource_type="platform_link",
        resource_id=link.id,
        detail=f"停用平台連結「{link.name}」",
        commit=True,
    )
    return {"message": "連結已停用"}


@router.delete("/{link_id}/purge")
def purge_link(
    link_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Hard-delete a platform link, irreversible.

    Lower stakes than user purge so this stays at admin tier (not owner-
    only). ``service_access_grant.platform_link_id`` already has
    ``ondelete=CASCADE`` so direct ``db.delete(link)`` collapses any
    per-user grant rows pointing at it.
    """
    link = db.query(PlatformLink).filter(PlatformLink.id == link_id).first()
    if not link:
        raise HTTPException(status_code=404, detail="連結不存在")
    name = link.name
    db.delete(link)
    db.commit()
    log_audit_event(
        db,
        actor=admin,
        action="purge",
        resource_type="platform_link",
        resource_id=link_id,
        detail=f"完全刪除平台連結「{name}」",
        commit=True,
    )
    return {"message": f"連結「{name}」已完全刪除"}
