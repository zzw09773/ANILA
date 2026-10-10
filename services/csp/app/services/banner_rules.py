"""公告橫幅的輸入形狀與 level 檢查。設定匯入與管理 API 共用。"""
from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel, Field

_LEVELS = {"info", "warning", "error", "success"}


class BannerCreate(BaseModel):
    level: str = Field(default="info")
    content: str = Field(min_length=1, max_length=2000)
    is_active: bool = True
    # 不傳就是不公開。管理員必須主動勾,才會出現在登入頁。
    show_on_login: bool = False
    sort_order: int = 0


def _validate_level(level: str) -> None:
    if level not in _LEVELS:
        raise HTTPException(status_code=400, detail=f"level 必須是 {', '.join(sorted(_LEVELS))}")
