"""儀表板磁碟與 HTTPS 憑證到期。

目標寫死：磁碟是既有的資料掛載，憑證是 compose 裡的 nginx。
不接受查詢參數，避免變成帶著管理員身分的內網探測。
回應不含宿主機路徑與私鑰。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.models.user import User
from app.services.alert_detectors import current_disk_mounts, public_disk_mounts
from app.services.auth_service import require_admin
from app.services.tls_certificate import public_certificate_view

router = APIRouter(prefix="/api/admin", tags=["容量"])


def _reject_query(request: Request) -> None:
    if request.query_params:
        raise HTTPException(status_code=400, detail="此端點不接受查詢參數")


@router.get("/disk-mounts")
def get_disk_mounts(
    request: Request, _admin: User = Depends(require_admin)
) -> list[dict]:
    _reject_query(request)
    return public_disk_mounts(current_disk_mounts())


@router.get("/tls-certificate")
def get_tls_certificate(
    request: Request, _admin: User = Depends(require_admin)
) -> dict:
    _reject_query(request)
    return public_certificate_view()
