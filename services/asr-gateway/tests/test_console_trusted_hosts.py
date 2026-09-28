"""治理中心加的語音主機，不必改 compose 的 ANILA_TRUSTED_HOSTS。"""
from __future__ import annotations

import pytest
import respx
from httpx import Response

from anila_core.security.url_guard import UnsafeEndpointError

from app.config import Settings
from app.console_trusted_hosts import refresh_console_trusted_hosts
from app.decode_endpoint import guard_decode_url


@pytest.mark.asyncio
@respx.mock
async def test_console_host_allows_the_decoder_when_env_list_is_empty(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "")
    respx.get("http://csp.test/api/internal/trusted-hosts").mock(
        return_value=Response(200, json={"hosts": ["asr-decoder"]}),
    )
    settings = Settings(
        CSP_BASE_URL="http://csp.test",
        CSP_SERVICE_TOKEN="csk-asr",
    )
    await refresh_console_trusted_hosts(settings, force=True)
    guard_decode_url("http://asr-decoder:9000")


@pytest.mark.asyncio
async def test_empty_console_cache_still_refuses_an_unlisted_decoder(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "")
    with pytest.raises(UnsafeEndpointError):
        guard_decode_url("http://asr-decoder:9000")
