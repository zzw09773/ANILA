"""儀表板「最後一次備份」—— ``GET /api/admin/backup-status``。

只給 admin / owner。回應是白名單：時間、結果、大小、是否超過 36 小時。
不回宿主機路徑，也不回備份腳本的錯誤原文。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.models.user import User
from app.services.auth_service import require_admin
from app.services.backup_status import assess_backup, public_backup_view

router = APIRouter(prefix="/api/admin", tags=["備份"])


@router.get("/backup-status")
def get_backup_status(_admin: User = Depends(require_admin)) -> dict:
    return public_backup_view(assess_backup())
