# -*- coding: utf-8 -*-
"""每個請求一個 id,回應頭帶出、log 記得到 —— 補救計畫 W3-3⑤。

## 為什麼需要

W2-12 的錯誤信封有 `request_id` 欄,但它先前**永遠是 None**:唯一來源是 inbound
header,而沒有人送。於是信封長這樣:

    {"error": {"code": "...", "message": "...", "request_id": null}}

使用者回報「我剛剛操作失敗」時,支援端沒有任何東西可以拿去 grep log。W2-12 刻意
不自己生 id(憑空造一個只存在於那個回應裡的 id,比沒有更糟),把真值留給本包。

本包補上:入站生成 → 存進 `request.state` 與 contextvar → 回應頭帶出 → 寫一行
access log。三者用**同一個值**,所以「使用者念出畫面上的 id」與「支援端 grep log」
會對上。

## ⚠ 為什麼要清洗 inbound header

沿用 client 送來的 id 是好事(跨服務追蹤),但那個值是**攻擊者可控字串**,而它
會進 log 檔與回應頭:

- 進 log → **log injection**。塞入換行就能偽造一整行 log(例如假造一筆
  「admin 授權成功」),而 log 是稽核證據。
- 進回應頭 → header injection(現代 ASGI 伺服器多半會擋,但不該倚賴那層)。

所以只接受 `[A-Za-z0-9._-]`、長度上限 64;不合格就**當作沒送**、自己生一個新的
(不是報錯 —— 一個格式不對的追蹤 id 不值得讓請求失敗)。
"""
from __future__ import annotations

import contextvars
import logging
import re
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

REQUEST_ID_HEADER = "X-Request-ID"

# 只允許不會破壞 log 一行一筆語意、也不會破壞 header 的字元。
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# 讓 log 在沒有 request 物件的深層程式碼裡也拿得到 id。
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "anila_request_id", default=None
)

_access_log = logging.getLogger("csp.access")


def current_request_id() -> str | None:
    """目前請求的 id;不在請求脈絡內時回 None。"""
    return request_id_var.get()


def _sanitize(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip()
    return candidate if _SAFE_ID.match(candidate) else None


class RequestIdMiddleware(BaseHTTPMiddleware):
    """入站生成 / 沿用 → contextvar → 回應頭 → 一行 access log。"""

    async def dispatch(self, request: Request, call_next):
        rid = _sanitize(request.headers.get(REQUEST_ID_HEADER)) or uuid.uuid4().hex
        request.state.request_id = rid
        token = request_id_var.set(rid)
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers[REQUEST_ID_HEADER] = rid
            return response
        finally:
            # 即使 handler 炸掉也要留下這一行 —— 「失敗的請求」正是最需要被 grep
            # 的那些。`finally` 而非 `else`,而且 status_code 預設 500。
            request_id_var.reset(token)
            _access_log.info(
                "%s %s %s %.1fms request_id=%s",
                request.method,
                request.url.path,
                status_code,
                (time.perf_counter() - started) * 1000.0,
                rid,
            )
