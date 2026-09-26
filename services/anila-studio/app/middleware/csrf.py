# -*- coding: utf-8 -*-
"""Cookie 工作階段的 double-submit CSRF。語意對齊 CSP 的 CsrfMiddleware。

瀏覽器會自動附上 httpOnly ``anila_access_token``，跨站頁面也能對 Studio
發出變更請求。非 httpOnly 的 ``anila_csrf`` 只有同源腳本讀得到，所以
變更請求必須帶相同的 ``X-CSRF-Token``。

豁免與 CSP 相同：
- GET / HEAD / OPTIONS
- ``Authorization: Bearer``（瀏覽器不會自動附上這個標頭）
- 沒有 access cookie 的請求（後面的認證回 401）
"""

from __future__ import annotations

import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

CSRF_COOKIE_NAME = "anila_csrf"
CSRF_HEADER_NAME = "x-csrf-token"
ACCESS_COOKIE_NAME = "anila_access_token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# 結尾沒有斜線的前綴只對整段路徑生效，避免 /health 連帶豁免 /healthz。
_EXEMPT_PREFIXES: tuple[str, ...] = ("/health",)


def _matches_exempt_prefix(path: str) -> bool:
    for prefix in _EXEMPT_PREFIXES:
        if prefix.endswith("/"):
            if path.startswith(prefix):
                return True
        elif path == prefix or path.startswith(prefix + "/"):
            return True
    return False


class CsrfMiddleware(BaseHTTPMiddleware):
    """Cookie 驗證的變更請求要比對 CSRF cookie 與標頭。"""

    async def dispatch(self, request: Request, call_next) -> Response:
        if _should_skip(request):
            return await call_next(request)

        cookie_token = request.cookies.get(CSRF_COOKIE_NAME) or ""
        header_token = request.headers.get(CSRF_HEADER_NAME) or ""
        # 長度不同也走 compare_digest，避免用提前返回透露長度。
        if (
            not cookie_token
            or not header_token
            or not hmac.compare_digest(cookie_token, header_token)
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": "CSRF 驗證失敗，請重新登入"},
            )
        return await call_next(request)


def _should_skip(request: Request) -> bool:
    if request.method.upper() in SAFE_METHODS:
        return True
    # 用路由路徑，不用 request.url.path。後者會跟著 Host 走
    # （CVE-2026-48710），攻擊者可以把豁免前綴拼到真正的路徑前面。
    if _matches_exempt_prefix(request.scope["path"]):
        return True
    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        return True
    if not request.cookies.get(ACCESS_COOKIE_NAME):
        return True
    return False
