"""治理中心加的語音主機，不必改 compose 的 ANILA_TRUSTED_HOSTS。"""
from __future__ import annotations

import pytest
import respx
from httpx import Response

from anila_core.security.url_guard import UnsafeEndpointError

from app.config import Settings
from app.console_trusted_hosts import (
    refresh_console_trusted_hosts,
    reset_console_trusted_hosts,
)
from app.decode_endpoint import guard_decode_url


@pytest.fixture(autouse=True)
def _clear_console_host_cache():
    reset_console_trusted_hosts()
    yield
    reset_console_trusted_hosts()


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
@respx.mock
async def test_removed_host_stops_passing_after_the_stale_window(monkeypatch):
    import app.console_trusted_hosts as hosts

    clock = {"now": 1_000.0}
    monkeypatch.setattr(hosts.time, "monotonic", lambda: clock["now"])
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "")
    calls = {"n": 0}

    def answer(_request):
        calls["n"] += 1
        if calls["n"] == 1:
            return Response(200, json={"hosts": ["asr-decoder"]})
        return Response(503, text="down")

    respx.get("http://csp.test/api/internal/trusted-hosts").mock(side_effect=answer)
    settings = Settings(
        CSP_BASE_URL="http://csp.test",
        CSP_SERVICE_TOKEN="csk-asr",
    )
    await refresh_console_trusted_hosts(settings, force=True)
    guard_decode_url("http://asr-decoder:9000")

    clock["now"] = 1_000.0 + 31
    await refresh_console_trusted_hosts(settings, force=True)
    guard_decode_url("http://asr-decoder:9000")

    clock["now"] = 1_000.0 + 301
    await refresh_console_trusted_hosts(settings, force=True)
    with pytest.raises(UnsafeEndpointError):
        guard_decode_url("http://asr-decoder:9000")


@pytest.mark.asyncio
async def test_empty_console_cache_still_refuses_an_unlisted_decoder(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "")
    with pytest.raises(UnsafeEndpointError):
        guard_decode_url("http://asr-decoder:9000")
