"""``/api/platform-links`` — compat façade over ``registered_services``.

Slice 7 (doc 07 §14): ``PlatformLink`` is superseded by ``RegisteredService``
but the legacy CRUD surface stays byte-compatible so existing CSP admin UI and
any callers keep working with zero changes. Every handler here now reads/writes
``registered_services`` and serialises through ``PlatformLinkResponse`` (the
RegisteredService ``.url`` property mirrors the old ``url`` column). New rows
are created with ``config_source="db"`` so the seed never clobbers them.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.registered_service import RegisteredService
from app.models.user import User
from app.schemas.platform_link import (
    PlatformLinkCreate,
    PlatformLinkResponse,
    PlatformLinkUpdate,
)
from app.schemas.service_icon import ALLOWED_SERVICE_ICONS
from app.services import anilalm_release_gate as release_gate
from app.services.access_control import accessible_links_for
from app.services.audit_service import log_audit_event
from app.services.auth_service import get_current_user, is_admin_tier, require_admin
from app.utils.slug import unique_slug

router = APIRouter(prefix="/api/platform-links", tags=["平台連結"])


@router.get("/icons")
def list_icons(_: User = Depends(require_admin)):
    """Icon allow-list for the governance picker. Static path must stay
    ahead of ``/{link_id}`` so ``icons`` is never captured as an id.
    """
    return {"icons": sorted(ALLOWED_SERVICE_ICONS)}


def _taken_slugs(db: Session) -> set[str]:
    return {row[0] for row in db.query(RegisteredService.slug).all()}


@router.get("", response_model=list[PlatformLinkResponse])
def list_links(
    include_inactive: bool = False,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # admin / owner 全可視 + include_inactive 切換;一般 user 走
    # access_control 的 role gate + grant check(include_inactive 靜默忽略)。
    #
    # Release gate:與 /api/services 同一條規則(見 app/api/services.py 的
    # list_services)—— 預設清單是使用者面的「可用服務」,閘門關著的不列;
    # include_inactive=true 的 admin 管理清單照列,否則管理員管不到。
    if is_admin_tier(current_user):
        query = db.query(RegisteredService).order_by(
            RegisteredService.sort_order, RegisteredService.created_at
        )
        if not include_inactive:
            query = query.filter(RegisteredService.is_active.is_(True))
        rows = query.all()
        return rows if include_inactive else release_gate.filter_available(rows)
    return release_gate.filter_available(accessible_links_for(db, current_user))


@router.post("", response_model=PlatformLinkResponse)
def create_link(
    request: PlatformLinkCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    data = request.model_dump()
    url = data.pop("url")
    service = RegisteredService(
        name=data["name"],
        slug=unique_slug(data["name"], _taken_slugs(db), fallback="link"),
        entry_url=url,
        icon=data.get("icon"),
        description=data.get("description"),
        sort_order=data.get("sort_order", 0),
        is_public=data.get("is_public", False),
        required_roles=data.get("required_roles") or [],
        config_source="db",
        db_editable_fields=[],
    )
    db.add(service)
    db.commit()
    db.refresh(service)
    log_audit_event(
        db,
        actor=admin,
        action="create",
        resource_type="platform_link",
        resource_id=service.id,
        detail=f"建立平台連結「{service.name}」",
        commit=True,
    )
    return service


@router.put("/{link_id}", response_model=PlatformLinkResponse)
def update_link(
    link_id: int,
    request: PlatformLinkUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    service = (
        db.query(RegisteredService)
        .filter(RegisteredService.id == link_id)
        .first()
    )
    if not service:
        raise HTTPException(status_code=404, detail="連結不存在")

    update_data = request.model_dump(exclude_unset=True)
    if "url" in update_data:
        service.entry_url = update_data.pop("url")
    for field, value in update_data.items():
        setattr(service, field, value)

    db.commit()
    db.refresh(service)
    log_audit_event(
        db,
        actor=admin,
        action="update",
        resource_type="platform_link",
        resource_id=service.id,
        detail=f"更新平台連結「{service.name}」",
        commit=True,
    )
    return service


@router.delete("/{link_id}")
def delete_link(
    link_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    service = (
        db.query(RegisteredService)
        .filter(RegisteredService.id == link_id)
        .first()
    )
    if not service:
        raise HTTPException(status_code=404, detail="連結不存在")
    service.is_active = False
    db.commit()
    log_audit_event(
        db,
        actor=admin,
        action="deactivate",
        resource_type="platform_link",
        resource_id=service.id,
        detail=f"停用平台連結「{service.name}」",
        commit=True,
    )
    return {"message": "連結已停用"}


@router.delete("/{link_id}/purge")
def purge_link(
    link_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Hard-delete a registered service, irreversible.

    Slice 7 preserve-history (doc §14 blocker): ``service_access_grants``
    reference the service via ``service_id`` with ``ON DELETE SET NULL`` and
    ``service_launches`` / ``service_audit_callbacks`` likewise, so the grant
    and launch/audit history rows SURVIVE this purge (their ``service_id`` is
    nulled, the audit trail is not erased) — unlike the old CASCADE that
    silently deleted grant rows.
    """
    service = (
        db.query(RegisteredService)
        .filter(RegisteredService.id == link_id)
        .first()
    )
    if not service:
        raise HTTPException(status_code=404, detail="連結不存在")
    name = service.name
    db.delete(service)
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
