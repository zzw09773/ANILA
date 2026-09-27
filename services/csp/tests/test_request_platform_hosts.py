"""CSP 把這次請求的 Host 當成平台網址，不靠寫死的實驗 IP。"""
from __future__ import annotations


def test_csp_app_binds_the_incoming_request_host():
    from app.main import app
    from anila_core.security.external_content import RequestPlatformHostsMiddleware

    assert RequestPlatformHostsMiddleware in [item.cls for item in app.user_middleware]
