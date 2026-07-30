"""Admin announcement banners API.

- ``GET /api/banners/active``  any authenticated user — banners the chat UI shows.
- ``GET/POST/PUT/DELETE /api/banners``  admin tier — manage banners.

Content is plain text; ``level`` drives the colour. No HTML execution.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.banner import Banner
from app.models.user import User
from app.services.audit_service import log_audit_event
from app.services.auth_service import get_current_user, is_admin_tier
from app.schemas.base import ApiResponseModel

router = APIRouter(prefix="/api/banners", tags=["公告橫幅"])

_LEVELS = {"info", "warning", "error", "success"}


class BannerCreate(BaseModel):
    level: str = Field(default="info")
    content: str = Field(min_length=1, max_length=2000)
    is_active: bool = True
    sort_order: int = 0


class BannerUpdate(BaseModel):
    level: Optional[str] = None
    content: Optional[str] = Field(default=None, min_length=1, max_length=2000)
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None


class BannerResponse(ApiResponseModel):
    id: int
    level: str
    content: str
    is_active: bool
    sort_order: int
    created_at: datetime
    model_config = {"from_attributes": True}


def _require_admin(user: User = Depends(get_current_user)) -> User:
    if not is_admin_tier(user):
        raise HTTPException(status_code=403, detail="需要管理員權限")
    return user


def _validate_level(level: str) -> None:
    if level not in _LEVELS:
        raise HTTPException(status_code=400, detail=f"level 必須是 {', '.join(sorted(_LEVELS))}")


def _client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    xff = request.headers.get("x-forwarded-for")
    return xff.split(",")[0].strip() if xff else (request.client.host if request.client else None)


@router.get("/active", response_model=list[BannerResponse])
def list_active_banners(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Active banners for the chat UI — any authenticated user."""
    rows = (
        db.query(Banner)
        .filter(Banner.is_active == True)  # noqa: E712
        .order_by(Banner.sort_order, Banner.id)
        .all()
    )
    return rows


@router.get("", response_model=list[BannerResponse])
def list_banners(
    current_user: User = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    return db.query(Banner).order_by(Banner.sort_order, Banner.id).all()


@router.post("", response_model=BannerResponse, status_code=201)
def create_banner(
    payload: BannerCreate,
    http_request: Request,
    current_user: User = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    _validate_level(payload.level)
    banner = Banner(
        level=payload.level,
        content=payload.content,
        is_active=payload.is_active,
        sort_order=payload.sort_order,
        created_by_user_id=current_user.id,
    )
    db.add(banner)
    db.commit()
    db.refresh(banner)
    log_audit_event(
        db,
        actor=current_user,
        action="create_banner",
        resource_type="banner",
        resource_id=banner.id,
        detail=f"張貼公告 [{payload.level}]",
        ip_address=_client_ip(http_request),
        commit=True,
    )
    return banner


@router.put("/{banner_id}", response_model=BannerResponse)
def update_banner(
    banner_id: int,
    payload: BannerUpdate,
    current_user: User = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    banner = db.query(Banner).filter(Banner.id == banner_id).first()
    if not banner:
        raise HTTPException(status_code=404, detail="公告不存在")
    if payload.level is not None:
        _validate_level(payload.level)
        banner.level = payload.level
    if payload.content is not None:
        banner.content = payload.content
    if payload.is_active is not None:
        banner.is_active = payload.is_active
    if payload.sort_order is not None:
        banner.sort_order = payload.sort_order
    db.commit()
    db.refresh(banner)
    return banner


@router.delete("/{banner_id}", status_code=204)
def delete_banner(
    banner_id: int,
    current_user: User = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    banner = db.query(Banner).filter(Banner.id == banner_id).first()
    if not banner:
        raise HTTPException(status_code=404, detail="公告不存在")
    db.delete(banner)
    db.commit()
