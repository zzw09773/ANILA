"""治理中心看到的平台版本。

版本來自映像裡的 VERSION。更新與回復的稽核由 anila-update.sh 直接寫進
資料庫，不經過這個 HTTP 端點。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.models.user import User
from app.services.auth_service import get_current_user
from app.services.platform_release import read_platform_version

router = APIRouter(prefix="/api/platform-version", tags=["平台版本"])


@router.get("")
def platform_version(_user: User = Depends(get_current_user)) -> dict:
    return {"version": read_platform_version()}
