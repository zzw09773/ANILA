"""OpenAI-compatible 端點相容層。

許多自架端點（vLLM / tensorrt-llm）以 pydantic ``extra='forbid'`` 驗證請求，會對
OpenAI Agents SDK 預設帶的 ``strict`` 欄位回 400（``extra_forbidden``）。SDK 的
chatcmpl 轉換器**無條件**在 ``tools[].function`` 加上 ``strict``（值為 True/False 都送），
故設 ``strict_mode=False`` 無法解決——必須在送出前把欄位整個剝掉。

本模組用一個 httpx transport 包裝器在 wire 層移除 ``strict``，與 SDK 內部解耦：
即使 SDK 版本變動，只要請求仍是 OpenAI-compatible 形狀就有效。本地的 strict schema
驗證仍保留（只是不送上 wire）。
"""

from __future__ import annotations

import json

import httpx


def strip_strict_fields(body: bytes) -> bytes | None:
    """移除 chat completions 請求 body 中的 ``strict`` 欄位。

    處理兩處已知位置：``tools[].function.strict`` 與
    ``response_format.json_schema.strict``。無變動時回 None（呼叫端可略過重建）。
    """
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    changed = False

    tools = data.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            fn = tool.get("function") if isinstance(tool, dict) else None
            if isinstance(fn, dict) and fn.pop("strict", _MISSING) is not _MISSING:
                changed = True

    rf = data.get("response_format")
    if isinstance(rf, dict):
        schema = rf.get("json_schema")
        if isinstance(schema, dict) and schema.pop("strict", _MISSING) is not _MISSING:
            changed = True

    if not changed:
        return None
    return json.dumps(data).encode("utf-8")


_MISSING = object()


class StripStrictTransport(httpx.AsyncBaseTransport):
    """包裝內層 transport，對 chat completions 請求剝除 ``strict`` 欄位。"""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/chat/completions"):
            new_body = strip_strict_fields(request.content)
            if new_body is not None:
                request = _rebuild(request, new_body)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _rebuild(request: httpx.Request, body: bytes) -> httpx.Request:
    """以新 body 重建請求；丟掉 content-length 讓 httpx 重算。"""
    headers = [(k, v) for k, v in request.headers.raw if k.lower() != b"content-length"]
    return httpx.Request(
        request.method,
        request.url,
        headers=headers,
        content=body,
        extensions=request.extensions,
    )


def build_http_client(verify: bool, timeout: float) -> httpx.AsyncClient:
    """建構會剝除 ``strict`` 的 httpx async client；``verify`` 接自 ANILA_SSL_VERIFY。"""
    inner = httpx.AsyncHTTPTransport(verify=verify)
    return httpx.AsyncClient(transport=StripStrictTransport(inner), timeout=timeout)
