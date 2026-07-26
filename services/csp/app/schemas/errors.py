"""結構化錯誤信封的 wire schema(W2-12)。

為什麼要有這個檔
----------------
在此之前 CSP 的錯誤回應沒有任何 schema:770 個 ``HTTPException`` 各自決定
``detail`` 是字串還是 dict,而 ``RequestValidationError`` 又吐 array。三個
前端只能各自猜形狀,猜錯就渲染成 ``[object Object]``。

信封本身刻意極小,只有四個欄位:

``code``
    穩定的機器可讀識別碼。**前端分流只准看這個**,不准比對 ``message``
    ——訊息是給人看的,會被改字、會被翻譯。
``message``
    給使用者看的單行訊息(繁中)。
``details``
    選填的結構化補充(例:pydantic 的逐欄位錯誤、配額數字)。
``request_id``
    W3-3⑤ 的 ``X-Request-ID`` 接點。**現在多半是 ``None``,但欄位必須存在**
    ——先把契約欄位定下來,前端與 log 對照的 UI 才不用等後端再改一次 schema。

``ErrorEnvelope.detail`` 是**過渡期雙寫**欄位:值與形狀與改動前逐字相同,
讓既有前端(governance UI 102 處直接插值 ``data.detail``)不會被一次切死。
規劃是**一個 release 後移除**;移除前 ``services/csp/tests/test_error_envelope.py``
會釘住它。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorBody(BaseModel):
    """信封內層。"""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(
        ...,
        description=(
            "穩定的機器可讀錯誤碼。前端分流只准依賴此欄位,"
            "不得比對 message 的自然語言內容。"
        ),
        examples=["AUTH_PENDING_APPROVAL"],
    )
    message: str = Field(
        ...,
        description="給使用者看的單行訊息(繁體中文)。",
        examples=["等待核准中，請通知 admin"],
    )
    details: Any | None = Field(
        default=None,
        description="選填的結構化補充,例如 pydantic 的逐欄位驗證錯誤。",
    )
    request_id: str | None = Field(
        default=None,
        description=(
            "對應 X-Request-ID 的追蹤識別碼(W3-3⑤)。"
            "目前僅在請求自帶該 header 時有值,欄位恆存在。"
        ),
    )


class ErrorEnvelope(BaseModel):
    """所有錯誤回應的統一形狀。

    ``detail`` 是 legacy 相容欄位(見模組 docstring),新前端不要讀它。
    """

    model_config = ConfigDict(extra="forbid")

    error: ErrorBody
    detail: Any | None = Field(
        default=None,
        description=(
            "【過渡期雙寫,將於下一個 release 移除】"
            "改動前的原始 detail,形狀與值逐字不變。新的呼叫端請讀 error。"
        ),
    )
