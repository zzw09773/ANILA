"""統一錯誤信封 + error code 列舉(W2-12)。

問題
----
CSP 有 770 個 ``HTTPException`` 而**零** ``add_exception_handler``
(量測:``grep -roE 'HTTPException\(' services/csp/app --include='*.py' | wc -l``
改動前 770、改動後 767 —— 少的 3 個是本包改成 ``ApiError`` 的登入路徑)。結果是同一
個後端至少三種錯誤形狀(字串 detail / dict detail / 422 array),三個前端各自
猜:``apps/csp-governance-ui`` 102 處直接把 ``data.detail`` 插進字串 →
dict/array 變 ``[object Object]``;``apps/anilalm`` 的 ``explainError`` 不處理
dict → 訊息整條丟失退化成 ``"503 Request failed"``。而 ``LoginView.vue`` 靠比對
後端中文子字串 ``'等待核准'`` 決定要不要顯示待核准頁——後端改一個字,登入
UX 靜默壞掉。這個缺陷已經造成過生產事故(見
``services/csp/app/services/startup_migrations.py:245-247`` 的註解)。

解法(刻意收窄,不做全面 refactor)
----------------------------------
1. 兩個 handler(``HTTPException`` / ``RequestValidationError``)把**所有**錯誤
   統一成 ``{"error": {...}, "detail": <legacy>}``。767 個 raise site 一行都不用
   改就進了信封。
2. ``ErrorCode`` **只**為「前端有分流需求」的路徑定值,其餘走 status code
   fallback。不追求覆蓋每個 raise site——那會失控。
3. ``detail`` 過渡期雙寫,形狀逐字不變。這是 breaking change 的緩衝,
   移除要等下一個 release。

怎麼在 raise site 掛 code
-------------------------
兩種都可以,選一種:

    raise ApiError(status_code=403, code=ErrorCode.CLEARANCE_INSUFFICIENT,
                   message="clearance 不足")

    # 既有 4 處 typed dict detail 的寫法,handler 一樣認得(dict 的 "code" 會被拉進信封)
    raise HTTPException(status_code=503,
                        detail={"code": "model_unhealthy", "message": "..."})
"""

from __future__ import annotations

import http
from enum import Enum
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

REQUEST_ID_HEADER = "x-request-id"


class ErrorCode(str, Enum):
    """機器可讀錯誤碼。

    **Wire 值 == 成員名稱**(有測試釘住),前端對照表不會因為改 enum 值而漂。

    分成兩區:
    - **分流碼**:前端會依它改變 UI 行為(顯示待核准頁、引導改按 SSO、
      跳分級說明…)。新增前先問「前端真的要分流嗎」,不然就用 generic。
    - **Generic 碼**:767 個既有 raise site 的 status-code fallback,
      只保證「有 code 可讀」,不承載語意。
    """

    # ── 分流碼:認證 / 登入 ──────────────────────────────────────────────
    AUTH_INVALID_CREDENTIALS = "AUTH_INVALID_CREDENTIALS"
    AUTH_PENDING_APPROVAL = "AUTH_PENDING_APPROVAL"
    AUTH_PENDING_REGISTRATION = "AUTH_PENDING_REGISTRATION"
    AUTH_LOCAL_PASSWORD_DISABLED = "AUTH_LOCAL_PASSWORD_DISABLED"
    AUTH_SOURCE_NOT_SUPPORTED = "AUTH_SOURCE_NOT_SUPPORTED"
    AUTH_CSRF_INVALID = "AUTH_CSRF_INVALID"

    # ── 分流碼:模型 / agent ─────────────────────────────────────────────
    MODEL_NOT_REGISTERED = "MODEL_NOT_REGISTERED"
    MODEL_UNHEALTHY = "MODEL_UNHEALTHY"
    AGENT_NOT_APPROVED = "AGENT_NOT_APPROVED"

    # ── 分流碼:分級 / 授權 ─────────────────────────────────────────────
    CLEARANCE_INSUFFICIENT = "CLEARANCE_INSUFFICIENT"
    COMPARTMENT_DENIED = "COMPARTMENT_DENIED"
    CLASSIFICATION_FORBIDDEN = "CLASSIFICATION_FORBIDDEN"

    # ── 分流碼:配額 / 逾時 ─────────────────────────────────────────────
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    RATE_LIMITED = "RATE_LIMITED"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"

    # ── Generic fallback(不承載語意,只保證有 code) ────────────────────
    VALIDATION_ERROR = "VALIDATION_ERROR"
    BAD_REQUEST = "BAD_REQUEST"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    HTTP_ERROR = "HTTP_ERROR"


#: status code → generic code。不在表內的走 ``HTTP_ERROR``。
_STATUS_FALLBACK: dict[int, ErrorCode] = {
    400: ErrorCode.BAD_REQUEST,
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    408: ErrorCode.UPSTREAM_TIMEOUT,
    409: ErrorCode.CONFLICT,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    422: ErrorCode.VALIDATION_ERROR,
    429: ErrorCode.RATE_LIMITED,
    500: ErrorCode.INTERNAL_ERROR,
    503: ErrorCode.SERVICE_UNAVAILABLE,
    504: ErrorCode.UPSTREAM_TIMEOUT,
}


class ApiError(HTTPException):
    """帶 error code 的 ``HTTPException``。

    刻意繼承 ``HTTPException``:既有的 ``except HTTPException`` 攔截、
    FastAPI 的 handler 註冊、測試裡的 ``pytest.raises(HTTPException)``
    全部照舊work,不需要跟著改。

    ``detail`` 設成 ``message``,所以 legacy 雙寫欄位仍是可讀字串
    ——舊前端拿到的東西沒有變差。
    """

    def __init__(
        self,
        status_code: int,
        code: ErrorCode | str,
        message: str,
        *,
        details: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.code = code.value if isinstance(code, ErrorCode) else str(code)
        self.message = message
        self.details = details


def _status_phrase(status_code: int) -> str:
    try:
        return http.HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


def _request_id(request: Request | None) -> str | None:
    """優先讀 middleware 放進 ``request.state`` 的值(W3-3⑤ 已補上)。

    順序很重要:`request.state.request_id` 是 `RequestIdMiddleware` 清洗過(或
    自己生成)的值,而**同一個值**會出現在回應頭與 access log。直接讀 raw header
    會拿到未清洗的攻擊者可控字串,而且在 client 沒送時是 None —— 那正是 W2-12
    當時 `request_id` 永遠為 null 的原因。

    退回讀 header 只為了「middleware 尚未掛上」的情境(例如某些單元測試直接
    呼叫 handler),此時仍做同一份清洗。
    """
    if request is None:
        return None
    from app.middleware.request_id import _sanitize  # 避免 import 迴圈

    state_value = getattr(getattr(request, "state", None), "request_id", None)
    if isinstance(state_value, str) and state_value:
        return state_value
    return _sanitize(request.headers.get(REQUEST_ID_HEADER))


def build_envelope(
    *,
    code: ErrorCode | str,
    message: str,
    details: Any | None = None,
    legacy_detail: Any,
    request: Request | None = None,
) -> dict[str, Any]:
    """組出 wire payload。

    ``legacy_detail`` 是**必填**且不給預設值:忘記雙寫應該在寫碼時就爆,
    不該安靜地把舊前端切死。
    """
    return {
        "error": {
            "code": code.value if isinstance(code, ErrorCode) else str(code),
            "message": message,
            "details": details,
            "request_id": _request_id(request),
        },
        "detail": legacy_detail,
    }


def envelope_response(
    *,
    status_code: int,
    code: ErrorCode | str,
    message: str,
    details: Any | None = None,
    legacy_detail: Any,
    request: Request | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=build_envelope(
            code=code,
            message=message,
            details=details,
            legacy_detail=legacy_detail,
            request=request,
        ),
        headers=headers,
    )


def _summarize_validation_errors(errors: list[Any]) -> str:
    """把 pydantic 的逐欄位錯誤壓成一行人看得懂的訊息。

    原本前端拿到的是 array,直接插值就是 ``[object Object]``。
    """
    parts: list[str] = []
    for item in errors[:3]:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        loc = item.get("loc") or ()
        # 去掉 "body" / "query" 這層,使用者不需要知道
        fields = [str(x) for x in loc if str(x) not in ("body", "query", "path")]
        where = ".".join(fields) if fields else "請求內容"
        parts.append(f"{where}: {item.get('msg', '格式不正確')}")
    summary = "；".join(parts)
    if len(errors) > 3:
        summary += f"（另有 {len(errors) - 3} 項）"
    return f"請求驗證失敗 — {summary}" if summary else "請求驗證失敗"


async def http_exception_handler(request: Request, exc: Exception) -> Response:
    """``HTTPException`` → 信封。

    覆蓋 Starlette 預設 handler。三件事必須保持與原本一致,否則會踩到
    協定層問題而非只是換個 body:

    - **204 / 304 / <200 不得有 body**(Starlette 原本行為)。
    - ``exc.headers`` 要原樣帶出(例:401 的 ``WWW-Authenticate``)。
    - ``detail`` 的形狀與值逐字不變。
    """
    # Starlette 的基底類,不是 ``fastapi.HTTPException`` —— route-not-found 與
    # method-not-allowed 由框架直接 raise 基底類(見 register_exception_handlers)。
    assert isinstance(exc, StarletteHTTPException)
    headers = getattr(exc, "headers", None)
    if exc.status_code < 200 or exc.status_code in (204, 304):
        return Response(status_code=exc.status_code, headers=headers)

    detail = exc.detail
    details: Any | None = None

    if isinstance(exc, ApiError):
        code: ErrorCode | str = exc.code
        message = exc.message
        details = exc.details
    elif isinstance(detail, dict):
        # 既有 4 處 typed dict detail 的好範本:code 直接晉升。
        # 3 處是字面 ``detail={"code","message"}``(``services/proxy/service.py``
        # :180,:582、``api/proxy.py:326``),第 4 處經 ``detail`` 變數傳入
        # (``api/models.py:357`` 的 ``untrusted_host``,治理 UI 會讀它的 ``host``)。
        raw_code = detail.get("code")
        code = str(raw_code) if raw_code else _fallback_code(exc.status_code)
        raw_message = detail.get("message")
        message = str(raw_message) if raw_message else _status_phrase(exc.status_code)
        extra = {k: v for k, v in detail.items() if k not in ("code", "message")}
        details = extra or None
    elif isinstance(detail, str):
        code = _fallback_code(exc.status_code)
        message = detail
    elif isinstance(detail, (list, tuple)):
        code = _fallback_code(exc.status_code)
        message = _summarize_validation_errors(list(detail))
        details = jsonable_encoder(detail)
    else:
        code = _fallback_code(exc.status_code)
        message = _status_phrase(exc.status_code)

    return envelope_response(
        status_code=exc.status_code,
        code=code,
        message=message,
        details=details,
        legacy_detail=jsonable_encoder(detail),
        request=request,
        headers=headers,
    )


def _fallback_code(status_code: int) -> ErrorCode:
    return _STATUS_FALLBACK.get(status_code, ErrorCode.HTTP_ERROR)


async def validation_exception_handler(request: Request, exc: Exception) -> Response:
    """``RequestValidationError`` → 同一個信封。

    原本 FastAPI 回 ``{"detail": [ ... ]}``,與 handler 的 ``{"detail": str}``
    是兩種形狀。這裡把 array 搬到 ``error.details``、補一行人看得懂的
    ``error.message``,而 ``detail`` 保持 FastAPI 原本的 array 不變。
    """
    assert isinstance(exc, RequestValidationError)
    errors = jsonable_encoder(exc.errors())
    return envelope_response(
        status_code=422,
        code=ErrorCode.VALIDATION_ERROR,
        message=_summarize_validation_errors(
            errors if isinstance(errors, list) else [errors]
        ),
        details=errors,
        legacy_detail=errors,
        request=request,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """把兩個 handler 掛上。

    ``add_exception_handler`` 而非 decorator:方便測試用隔離 app 驗證,
    也讓「這裡總共只掛了兩個」在一個地方看得完。

    ⚠ **``StarletteHTTPException`` 這行不能省。** ``fastapi.HTTPException`` 是
    ``starlette.exceptions.HTTPException`` 的**子類**,而 Starlette 的 handler
    查表是走例外類別的 MRO。只註冊子類的話,route-not-found(404)與
    method-not-allowed(405)——由框架直接 raise **基底類** —— 會落回 FastAPI
    預設 handler 回 ``{"detail": ...}``,於是前端最常撞到的兩個錯誤反而是唯一
    沒有 ``error.code`` 的。實測踩過,由 ``test_error_envelope.py`` 的
    ``test_framework_raised_404_and_405_are_also_enveloped`` 釘住。
    """
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)


def install_error_envelope_schema(app: FastAPI) -> None:
    """把信封 schema 塞進 OpenAPI ``components.schemas``。

    為什麼要手動塞:信封是 handler 產生的,不掛在任何 route 的
    ``response_model`` 上,所以 FastAPI 的 schema 收集器看不到它。
    不塞的話匯出的 OpenAPI 完全沒有錯誤契約——而「契約層」正是本包要補的東西。

    刻意**不**在每個 route 加 ``default`` response:那會讓 273 條路由的
    OpenAPI 全部長大,審 diff 的成本遠高於收益。
    """
    from app.schemas.errors import ErrorBody, ErrorEnvelope

    original = app.openapi

    def openapi_with_error_envelope() -> dict[str, Any]:
        schema = original()
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        for model in (ErrorBody, ErrorEnvelope):
            components.setdefault(
                model.__name__,
                model.model_json_schema(
                    ref_template="#/components/schemas/{model}"
                ),
            )
        return schema

    app.openapi = openapi_with_error_envelope  # type: ignore[method-assign]
