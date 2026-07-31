# -*- coding: utf-8 -*-
"""同仁通訊錄 —— 一般使用者查得到的「姓名＋單位」，僅此而已。

擁有者裁決(OWNER-QUESTIONS Q9，2026-07-31 早，選項②)：全院都查得到，
但只回姓名與單位。原話：「這些資訊其實在我們 outlook 都找得到所以沒關係」。
既有資訊管道已公開的東西，在這裡再鎖一次只會擋到自己人。

**為什麼另開一支窄端點，而不是放寬 `GET /api/users`**：那支是管理端點，
回的是完整 `UserResponse`(email、角色、核准狀態、最後登入時間…)。在寬
端點上加一層過濾器，只要有人日後多加一個回傳欄位就會連帶外洩；這裡的
查詢**只 select 三個欄位**、回應模型**只宣告三個欄位**，要多回東西必須
改這個檔案，改了就會撞到 `test_directory_lookup.py` 的欄位集斷言。

`id` 是三個欄位裡唯一不是「姓名或單位」的：它是交接／分享挑人時必須帶回
後端的把手(`to_user_id`)。沒有它，前端就只能送自由文字，那正是 2026-07-30
被拿掉的那顆假按鈕的成因。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.department import Department
from app.models.user import User
from app.services.auth_service import get_current_user

router = APIRouter(prefix="/api/directory", tags=["同仁通訊錄"])

# 一次最多回幾筆。挑人用的清單，不是匯出用的名冊 —— 上限低一點，
# 想抓全院名單的人得自己翻頁，翻頁會留下正常的存取紀錄。
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 50


class DirectoryEntry(BaseModel):
    """通訊錄單筆 —— 欄位就是這三個，不要再加。

    加欄位＝擴大一般使用者看得到的個資範圍，那是擁有者的決定，不是
    實作者的。真的要加，先回 OWNER-QUESTIONS 問過。
    """

    id: int
    username: str
    department: str | None = None


@router.get("/users", response_model=list[DirectoryEntry])
def search_directory(
    q: str = Query("", max_length=100, description="帳號關鍵字；留空回前幾筆"),
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[DirectoryEntry]:
    """查同仁。任何已登入的使用者都能查(Q9)；未登入者拿不到任何東西。

    只列**在職且已核准**的帳號 —— 停用/待審的帳號出現在挑人清單裡，
    使用者會挑了才發現交不出去。自己不列，交接給自己沒有意義。
    """
    rows = (
        db.query(User.id, User.username, Department.name)
        .outerjoin(Department, User.department_id == Department.id)
        .filter(
            User.is_active.is_(True),
            User.is_approved.is_(True),
            User.id != current_user.id,
        )
    )
    keyword = q.strip()
    if keyword:
        rows = rows.filter(User.username.ilike(f"%{keyword}%"))
    rows = rows.order_by(User.username).limit(limit).all()
    return [
        DirectoryEntry(id=row[0], username=row[1], department=row[2])
        for row in rows
    ]
