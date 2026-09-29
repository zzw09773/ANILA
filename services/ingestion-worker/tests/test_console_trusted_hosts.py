"""worker 的私網放行名單來自治理中心，不再靠 ANILA_WORKER_TRUSTED_HOSTS。

先前 compose 只放行 docling，遠端 Docling（私網 IP）一律被 SSRF 檢查擋下，
文件卡在解析、0 個區塊。
"""
from __future__ import annotations

import httpx
import pytest

from anila_core.security.url_guard import (
    ENDPOINT_KIND_MODEL,
    UnsafeEndpointError,
    validate_outbound_url,
)
from ingestion_worker import console_trusted_hosts as cth

DOCLING = "http://172.16.120.35:9100"


@pytest.fixture(autouse=True)
def _private_http_allowed(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "docling")
    cth.reset_for_tests()
    yield
    cth.reset_for_tests()


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_console_host_lets_the_private_docling_through():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["path"] = request.url.path
        return httpx.Response(200, json={"hosts": ["172.16.120.35"]})

    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url(DOCLING, ENDPOINT_KIND_MODEL)

    async with _client(handler) as client:
        await cth.refresh_console_trusted_hosts(
            "http://csp:8000", "sk-worker", http_client=client, force=True
        )

    assert seen == {"auth": "Bearer sk-worker", "path": "/api/internal/trusted-hosts"}
    validate_outbound_url(DOCLING, ENDPOINT_KIND_MODEL)


@pytest.mark.asyncio
async def test_hosts_are_dropped_after_five_minutes_without_csp(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(cth.time, "monotonic", lambda: clock[0])

    async with _client(lambda r: httpx.Response(200, json={"hosts": ["172.16.120.35"]})) as ok:
        await cth.refresh_console_trusted_hosts("http://csp:8000", "sk-worker", http_client=ok, force=True)
    validate_outbound_url(DOCLING, ENDPOINT_KIND_MODEL)

    async with _client(lambda r: httpx.Response(503)) as down:
        clock[0] += 60
        await cth.refresh_console_trusted_hosts("http://csp:8000", "sk-worker", http_client=down, force=True)
        validate_outbound_url(DOCLING, ENDPOINT_KIND_MODEL)  # 短暫失敗：沿用上一筆
        clock[0] += 400
        await cth.refresh_console_trusted_hosts("http://csp:8000", "sk-worker", http_client=down, force=True)

    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url(DOCLING, ENDPOINT_KIND_MODEL)
