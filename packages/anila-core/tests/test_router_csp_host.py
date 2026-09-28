"""Router 轉給 CSP 的 Host 只能是 nginx 傳進來的那一個。"""
from __future__ import annotations

import asyncio

from anila_core.api import router_server as rs


def test_csp_host_comes_from_the_request_host_not_a_smuggled_header():
    captured: dict = {}

    class _Client:
        async def get(self, url, headers=None, timeout=None):
            captured["headers"] = dict(headers or {})

            class _Resp:
                status_code = 200

            return _Resp()

    original = rs.get_http_client
    rs.get_http_client = lambda: _Client()
    token = rs._CSP_CLIENT_HOST.set("anila.example:443")
    try:
        asyncio.run(rs._csp_service_get("http://csp:8000/api/x", "tok"))
    finally:
        rs._CSP_CLIENT_HOST.reset(token)
        rs.get_http_client = original

    assert captured["headers"]["X-Forwarded-Host"] == "anila.example:443"
    assert captured["headers"]["X-CSP-Service-Token"] == "tok"


def test_smuggled_forwarded_host_is_replaced_and_other_headers_stay():
    token = rs._CSP_CLIENT_HOST.set("anila.example:443")
    try:
        headers = rs._with_csp_host({
            "Authorization": "Bearer x",
            "X-Forwarded-Host": "evil.example",
            "X-ANILA-Trace-Id": "t-1",
        })
    finally:
        rs._CSP_CLIENT_HOST.reset(token)
    assert headers["X-Forwarded-Host"] == "anila.example:443"
    assert headers["X-ANILA-Trace-Id"] == "t-1"
    assert headers["Authorization"] == "Bearer x"


def test_without_a_request_host_a_smuggled_value_is_not_forwarded():
    headers = rs._with_csp_host({"X-Forwarded-Host": "evil.example", "Authorization": "Bearer x"})
    assert not any(key.lower() == "x-forwarded-host" for key in headers)
    assert headers["Authorization"] == "Bearer x"


def test_middleware_reads_host_and_ignores_client_x_forwarded_host():
    seen: list[str] = []

    async def app(scope, receive, send):
        seen.append(rs._CSP_CLIENT_HOST.get())
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = rs._CaptureClientHostMiddleware(app)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    asyncio.run(middleware({
        "type": "http",
        "headers": [
            (b"host", b"anila.example"),
            (b"x-forwarded-host", b"evil.example"),
        ],
    }, receive, send))
    assert seen == ["anila.example"]
    assert rs._CSP_CLIENT_HOST.get() == ""
