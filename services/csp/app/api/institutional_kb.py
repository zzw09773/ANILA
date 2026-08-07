# -*- coding: utf-8 -*-
"""院內規章檢索的分數門檻：一個設定，外加一個看得到證據的校準視圖。

* ``GET  /api/institutional-kb/threshold``  目前門檻 ＋ 有沒有人真的量過
* ``PUT  /api/institutional-kb/threshold``  改門檻（留稽核）
* ``POST /api/institutional-kb/preview``    拿一句問題跑一次正式檢索，回實際分數

為什麼門檻是設定而不是常數
==========================
「找不到」與「找到了」之間隔著一個數字，而那個數字**取決於嵌入模型**。手上
的 0.3 是拿替代模型量出來的（``PLAN.md:77``），院內真正上線的是 nv-embed，
分數分布不一樣；模型還可能再換。把它寫死成常數，等於把「重新量一次」變成
一件要改程式碼、重建映像、重啟容器才做得到的事——也就是不會有人做。

⚠ **改了畫面就要改到行為。** 這是「設定頁」那件工程的第一塊磚，後面還有 41 個
環境變數要照這個形狀搬進來。所以門檻的讀取（``get_kb_threshold``）是**每次
請求真的查一次 DB**，沒有任何行程生命期的快取。一個「只是為了效能」的模組層
快取會讓設定頁上的每一個開關都變成假控制項，而且不會有任何錯誤訊息。

⚠ **校準視圖走的是正式檢索那條路**（``services/institutional_kb.py`` 的
``retrieve_institutional``），不是自己寫一份比較寬鬆的查詢。一旦分岔，管理員
看到的分數就不是使用者提問時會拿到的東西，而分岔最先掉的一定是密等過濾。

⚠ **preview 會套用目前的門檻**，因為它要誠實反映使用者會看到什麼。要看門檻
**以下**的分數以決定門檻該設多少時，做法是先把門檻設成 0、跑一次 preview 看
分數、再設定真正的值。這裡刻意不提供「只看不存」的臨時門檻參數：多一條參數
就多一條「校準時看到的」與「上線後跑的」可以不一樣的路。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.platform_setting import (
    KB_THRESHOLD_DEFAULT,
    KB_THRESHOLD_KEY,
    PlatformSetting,
    get_kb_threshold,
    is_kb_threshold_calibrated,
    set_kb_threshold,
)
from app.models.user import User
from app.schemas.base import ApiResponseModel
from app.services.audit_service import log_audit_event
from app.services.auth_service import require_admin
from app.services.institutional_kb import retrieve_institutional
from app.utils.client_ip import client_ip as _client_ip

# 整個 router 都是 admin 限定：門檻決定全院檢索的鬆緊（密等相鄰），而 preview
# 會把院規內容整段回出來。讀與寫同一道門。
router = APIRouter(
    prefix="/api/institutional-kb",
    tags=["院內規章知識庫"],
    dependencies=[Depends(require_admin)],
)


class ThresholdUpdate(BaseModel):
    # 值域檢查刻意放在端點裡而不是 ``Field(ge=..., le=...)``：pydantic 的
    # 422 訊息是英文的結構化錯誤，而本專案的規矩是「每一次拒絕都要給做法」。
    value: float


class ThresholdResponse(ApiResponseModel):
    value: float
    # ⚠ 有沒有人真的拿上線的嵌入模型量過。預設值是用替代模型量的，把它當成
    # 已知數就再也沒有人會去量。
    calibrated: bool
    default: float
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None


class PreviewRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)


class PreviewHit(ApiResponseModel):
    collection_id: int
    document_id: int
    filename: str
    content: str
    score: float


class PreviewResponse(ApiResponseModel):
    query: str
    threshold: float
    # ``KbState`` 的字串值。``not_searched`` 與 ``searched_miss`` 分開講，是因為
    # 「一個庫都沒標記」的人如果看到「搜過了、沒東西」，會去調門檻調一整個下午。
    state: str
    hits: list[PreviewHit]
    # 哪幾個庫沒查成。只說 partial_error 而不說是哪一個，管理員無從下手。
    failed_collections: list[int]


def _threshold_payload(db: Session) -> ThresholdResponse:
    row = db.get(PlatformSetting, KB_THRESHOLD_KEY)
    updated_by = None
    if row is not None and row.updated_by_user_id is not None:
        actor = db.get(User, row.updated_by_user_id)
        updated_by = actor.username if actor is not None else None
    return ThresholdResponse(
        value=get_kb_threshold(db),
        calibrated=is_kb_threshold_calibrated(db),
        default=KB_THRESHOLD_DEFAULT,
        updated_at=row.updated_at if row is not None else None,
        updated_by=updated_by,
    )


@router.get("/threshold", response_model=ThresholdResponse)
def read_threshold(db: Session = Depends(get_db)) -> ThresholdResponse:
    """目前生效的門檻。``calibrated=false`` 表示這個數字還沒有人量過。"""
    return _threshold_payload(db)


@router.put("/threshold", response_model=ThresholdResponse)
def update_threshold(
    payload: ThresholdUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> ThresholdResponse:
    """改門檻。下一次檢索就會用新值——不需要重啟，也沒有生效延遲。"""
    previous = get_kb_threshold(db)
    was_calibrated = is_kb_threshold_calibrated(db)
    try:
        set_kb_threshold(db, payload.value, actor=current_user)
    except ValueError:
        # 相似度分數的定義域是 [0, 1];界外值不是「比較嚴格」,是壞掉。
        # 訊息要帶合法範圍與怎麼決定這個數字,不是只說不行。
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"分數門檻必須介於 0.0 與 1.0 之間（收到 {payload.value}）。"
                "要決定填多少：先把門檻設成 0.0，用 "
                "POST /api/institutional-kb/preview 拿實際問題跑一次，"
                "看回傳的 score 落在哪裡再定。"
            ),
        )
    log_audit_event(
        db,
        commit=True,
        actor=current_user,
        action="platform_setting_set",
        resource_type="platform_setting",
        resource_id=KB_THRESHOLD_KEY,
        ip_address=_client_ip(request),
        metadata={
            "from": previous,
            "to": float(payload.value),
            "was_calibrated": was_calibrated,
        },
    )
    return _threshold_payload(db)


@router.post("/preview", response_model=PreviewResponse)
async def preview_retrieval(
    payload: PreviewRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> PreviewResponse:
    """拿一句真的問題跑一次**正式**檢索，把分數與命中內容原樣回出來。

    ``current_user`` 一路傳到 ``retrieve_institutional``：查詢的 embedding 走
    CSP proxy，用量記在人身上（``token_usage``）。校準也是有人在用模型，算他的。
    """
    threshold = get_kb_threshold(db)
    result = await retrieve_institutional(
        db, current_user, payload.query, threshold=threshold
    )
    return PreviewResponse(
        query=payload.query,
        threshold=threshold,
        state=result.state.value,
        hits=[
            PreviewHit(
                collection_id=hit.collection_id,
                document_id=hit.document_id,
                filename=hit.filename,
                content=hit.content,
                score=hit.score,
            )
            for hit in result.hits
        ],
        failed_collections=list(result.failed_collections),
    )
