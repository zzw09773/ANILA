"""Admin announcement banners API.

- ``GET /api/banners/public``  **no auth** — the opted-in subset the login page shows.
- ``GET /api/banners/active``  any authenticated user — banners the chat UI shows.
- ``GET/POST/PUT/DELETE /api/banners``  admin tier — manage banners.

Content is plain text; ``level`` drives the colour. No HTML execution.

為什麼會有 ``/public``
=====================
等待核准的人還沒有帳號,拿不到 token,所以 ``/active`` 對他們一律 401 ——
擁有者貼在治理中心的公告,漏掉的正好是最需要看到它的那一群人。``/public``
把那條路打通,代價是內容變成任何連得到登入頁的人都讀得到,因此是逐則
opt-in(``banners.show_on_login``),而且走一個只有兩個欄位的窄 schema。
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
from app.utils.client_ip import client_ip as _client_ip

router = APIRouter(prefix="/api/banners", tags=["公告橫幅"])

_LEVELS = {"info", "warning", "error", "success"}


class BannerCreate(BaseModel):
    level: str = Field(default="info")
    content: str = Field(min_length=1, max_length=2000)
    is_active: bool = True
    # 不傳就是不公開。管理員必須主動勾,才會出現在登入頁。
    show_on_login: bool = False
    sort_order: int = 0


class BannerUpdate(BaseModel):
    level: Optional[str] = None
    content: Optional[str] = Field(default=None, min_length=1, max_length=2000)
    is_active: Optional[bool] = None
    show_on_login: Optional[bool] = None
    sort_order: Optional[int] = None


class BannerResponse(ApiResponseModel):
    id: int
    level: str
    content: str
    is_active: bool
    show_on_login: bool
    sort_order: int
    created_at: datetime
    model_config = {"from_attributes": True}


class PublicBannerResponse(BaseModel):
    """登入頁公告的公開投影 —— 未認證的人看得到的**全部**。

    形狀對齊既有的 ``PublicAuthProviderResponse``(``/api/auth/providers``):
    管理面用 ``BannerResponse``,對外另立一個窄 schema,而不是讓同一個
    model 兼差。``id`` / ``is_active`` / ``sort_order`` / ``created_at`` /
    ``created_by_user_id`` 對還沒登入的人沒有用途,就不送出去。

    ``from_banner`` 是**唯一**的構造入口,而且「這則公告可不可以公開」只在
    這裡判斷一次。刻意不在 SQL 那邊再 filter 一次:兩道互相遮蔽的守衛,拆掉
    任何一道測試都不會紅,等於兩道都沒被驗證過。守衛只有一道,它就必須是對的,
    而且測試殺得死它。
    """

    level: str
    content: str

    @classmethod
    def from_banner(cls, banner: Banner) -> Optional["PublicBannerResponse"]:
        """沒有 opt-in(或已停用)的公告,在這裡連物件都組不出來。"""
        if not banner.show_on_login or not banner.is_active:
            return None
        return cls(level=banner.level, content=banner.content)


def _require_admin(user: User = Depends(get_current_user)) -> User:
    if not is_admin_tier(user):
        raise HTTPException(status_code=403, detail="需要管理員權限")
    return user


def _validate_level(level: str) -> None:
    if level not in _LEVELS:
        raise HTTPException(status_code=400, detail=f"level 必須是 {', '.join(sorted(_LEVELS))}")


@router.get("/public", response_model=list[PublicBannerResponse])
def list_public_banners(db: Session = Depends(get_db)):
    """登入頁公告 —— **不需要登入**,這正是重點。

    看得到這支回應的人包含「刷了卡、還在等核准、沒有帳號」的人。他們原本
    在登入頁上是一條死路:被告知等管理員核准,然後沒有任何可以問的對象。

    只回 ``show_on_login`` 勾過的那幾則(判斷在
    ``PublicBannerResponse.from_banner``);排序沿用管理員排好的
    ``sort_order, id``,登入頁取第一則。
    """
    rows = db.query(Banner).order_by(Banner.sort_order, Banner.id).all()
    public = (PublicBannerResponse.from_banner(row) for row in rows)
    return [row for row in public if row is not None]


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
        show_on_login=payload.show_on_login,
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
        detail=(
            f"張貼公告 [{payload.level}]"
            + ("(登入頁公開)" if payload.show_on_login else "")
        ),
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
    if payload.show_on_login is not None:
        banner.show_on_login = payload.show_on_login
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
