"""批次操作開始時畫面上的使用者。

新介面的批次刪除對話、改資料夾、批次刪摘要、清空摘要，會帶
X-ANILA-Expected-User-ID，值是按下操作時的 user.id。同一個瀏覽器
若另一個分頁已換成別的帳號，cookie 是新帳號，這個 header 仍是舊畫面。
兩邊不同就 409，而且在查資料、寫墓碑、改任何列之前拒絕。
沒有 header 的舊客戶端維持原行為。不從 body 的 user id 換身分。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException

from app.models.user import User

ACCOUNT_CHANGED = "登入帳號已變更，請重新整理後再操作"


def read_expected_user_id(
    x_anila_expected_user_id: Annotated[
        int | None,
        Header(alias="X-ANILA-Expected-User-ID", gt=0),
    ] = None,
) -> int | None:
    """省略 header 回 None。不是正整數時由 FastAPI 回 422。"""
    return x_anila_expected_user_id


def reject_if_expected_user_mismatch(
    current_user: User,
    expected_user_id: int | None,
) -> None:
    if expected_user_id is None:
        return
    if int(expected_user_id) != int(current_user.id):
        raise HTTPException(status_code=409, detail=ACCOUNT_CHANGED)


ExpectedUserId = Annotated[int | None, Depends(read_expected_user_id)]
