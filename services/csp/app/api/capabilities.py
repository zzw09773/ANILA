"""部署能力旗標(W1-3 ②③)—— ``GET /api/capabilities``。

為什麼需要這個端點
------------------
CSP 有 runtime feature flag 會**整段關掉**使用者看得到的功能:

* ``ENABLE_MEMORY``(``config.py`` 預設 ``False``;卡登部署明確 false)
* ``ENABLE_PUBLIC_SHARE``(卡登部署 false)

而前端過去無條件把功能講成「有」——記憶 tab 甚至寫「平台會自動學習」。
功能沒開時那句話是假的(稽核 S5「UI 字面與實作相反」缺陷類別)。前端必須
先問部署開了什麼,才能決定怎麼說。

設計紅線:白名單
----------------
回應**只**能是這兩個布林旗標。任何其他部署事實(profile 名稱、版本號、
資料庫位置、pilot 姿態…)都不准出現 —— 這個端點只要求登入即可讀,把部署
指紋放進來等於送給任何一個一般使用者。因此:

* ``CAPABILITY_FLAGS`` 是唯一的鍵來源(前端 ``runtime/capabilities.js``
  有一份逐字一致的白名單,由 ``tests/test_capabilities_api.py`` 釘死);
* response model 明確列欄位並 ``extra="forbid"``,不從 ``settings`` 反射
  任何東西 —— 新增旗標必須有人手動改這個檔並被 review 看到。

授權:任何已登入使用者(這是自己的 UI 要用的資訊),不需要 admin。
未登入不得探測部署姿態。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from app.config import settings
from app.models.user import User
from app.services.auth_service import get_current_user

router = APIRouter(tags=["部署能力旗標"])

# 端點允許回傳的鍵。前端白名單必須與此逐字一致。
CAPABILITY_FLAGS: tuple[str, ...] = ("enable_memory", "enable_public_share")


class CapabilitiesResponse(BaseModel):
    """只有布林旗標。extra="forbid" 讓「順手多塞一個欄位」在測試前就爆。"""

    model_config = ConfigDict(extra="forbid")

    enable_memory: bool
    enable_public_share: bool


@router.get(
    "/api/capabilities",
    response_model=CapabilitiesResponse,
    summary="這個部署開啟了哪些使用者可見功能",
)
def get_capabilities(
    _current_user: User = Depends(get_current_user),
) -> CapabilitiesResponse:
    return CapabilitiesResponse(
        enable_memory=bool(settings.ENABLE_MEMORY),
        enable_public_share=bool(settings.ENABLE_PUBLIC_SHARE),
    )
