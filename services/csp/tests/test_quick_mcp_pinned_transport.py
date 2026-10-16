"""私有端點查核所得 IP 必須用於實際傳輸，保留 Host 與 TLS 主機名稱。"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import HTTPException

from app.services import quick_mcp as mcp


@pytest.mark.parametrize(
    ("url", "address", "host"),
    [
        ("https://attendance.local/mcp", "10.20.30.40", "attendance.local"),
        ("https://attendance.local:8443/mcp", "10.20.30.40", "attendance.local:8443"),
        ("http://attendance.local:8080/mcp", "fd00::20", "attendance.local:8080"),
        ("https://[fd00::20]:8443/mcp", "fd00::20", "[fd00::20]:8443"),
    ],
)
def test_request_uses_checked_ip_without_second_hostname_resolution(
    monkeypatch, url, address, host
):
    hostname = httpx.URL(url).host
    resolutions = []
    sent = []
    monkeypatch.setattr(mcp, "get_cached_hosts", lambda: {hostname})
    monkeypatch.setattr(mcp, "_env_trusted_hosts", lambda: set())
    monkeypatch.setattr(mcp, "validate_outbound_url", lambda *args, **kwargs: None)

    def resolve(*_):
        resolutions.append(True)
        candidate = address if len(resolutions) == 1 else "8.8.8.8"
        return [(None, None, None, None, (candidate, 443))]

    monkeypatch.setattr(mcp, "_resolve_host", resolve)

    def respond(request):
        sent.append(request)
        assert request.url.host == address
        assert request.headers["host"] == host
        assert request.extensions["sni_hostname"] == hostname
        assert request.headers["authorization"] == "Bearer synthetic"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    async def scenario():
        client = mcp._MCPClient(url, "synthetic")
        await client.client.aclose()
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            assert await client.rpc("tools/list", {}) == {}
        finally:
            await client.client.aclose()

    asyncio.run(scenario())
    assert len(sent) == 1
    assert len(resolutions) == (0 if hostname == address else 1)


@pytest.mark.parametrize("addresses", [["8.8.8.8"], ["10.20.30.40", "8.8.8.8"], []])
def test_public_or_mixed_dns_is_rejected_before_transport(monkeypatch, addresses):
    monkeypatch.setattr(mcp, "get_cached_hosts", lambda: {"attendance.local"})
    monkeypatch.setattr(mcp, "_env_trusted_hosts", lambda: set())
    monkeypatch.setattr(mcp, "validate_outbound_url", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        mcp,
        "_resolve_host",
        lambda *_: [(None, None, None, None, (value, 443)) for value in addresses],
    )
    with pytest.raises(HTTPException) as denied:
        mcp._guard_endpoint("https://attendance.local/mcp")
    assert denied.value.detail["error_type"] == "mcp_endpoint_untrusted"


@pytest.mark.parametrize("address", [None, "attendance.local", "fd00::1%eth0"])
def test_missing_or_nonliteral_pin_fails_closed(monkeypatch, address):
    async def guard(_):
        return address

    monkeypatch.setattr(mcp, "_guard_endpoint_async", guard)

    async def scenario():
        client = mcp._MCPClient("https://attendance.local/mcp", "synthetic")
        await client.client.aclose()

        def unexpected_transport(_):
            pytest.fail("An invalid address pin must be rejected before transport")

        client.client = httpx.AsyncClient(
            transport=httpx.MockTransport(unexpected_transport)
        )
        try:
            with pytest.raises(HTTPException) as denied:
                await client.rpc("tools/list", {})
            assert denied.value.detail["error_type"] == "mcp_endpoint_untrusted"
        finally:
            await client.client.aclose()

    asyncio.run(scenario())
