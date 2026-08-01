"""Call-time SSRF re-validation on the proxy data plane (#117 TOCTOU).

Agent / model endpoint_url is validated at registration, but DNS rebinding can
change the resolved address between then and the actual forward. proxy_service
now re-runs the central guard at call time (_guard_outbound) inside
proxy_request / proxy_stream, raising HTTPException(502) before any outbound
request. Trusted hosts (ANILA_TRUSTED_HOSTS) short-circuit cheaply.

These tests drive the guard directly and through proxy_request so no real
network or DB is touched (the guard fires before either).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services.proxy_service import _guard_outbound, proxy_request


def test_guard_blocks_cloud_metadata_ip():
    with pytest.raises(HTTPException) as exc:
        _guard_outbound("https://169.254.169.254/v1/chat/completions")
    assert exc.value.status_code == 502


def test_guard_blocks_loopback_hostname():
    with pytest.raises(HTTPException) as exc:
        _guard_outbound("https://localhost/v1/chat/completions")
    assert exc.value.status_code == 502


def test_guard_blocks_single_label_service_name():
    # A bare docker service name is never a legitimate public endpoint.
    with pytest.raises(HTTPException) as exc:
        _guard_outbound("https://csp-db/v1/chat/completions")
    assert exc.value.status_code == 502


def test_guard_allows_trusted_host(monkeypatch):
    """An admin-blessed internal host (ANILA_TRUSTED_HOSTS) passes cheaply."""
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "gemma4")
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    # Should not raise.
    _guard_outbound("http://gemma4:8000/v1/chat/completions")


def test_proxy_request_blocks_unsafe_model_before_network():
    """proxy_request aborts with 502 at the guard — no httpx call attempted."""
    model = SimpleNamespace(
        endpoint_url="https://169.254.169.254",
        model_type="chat",
        api_version="v1",
        name="evil",
        id=1,
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            proxy_request(
                model=model,
                api_key_id=1,
                user_id=1,
                department_id=None,
                request_body={"messages": []},
                endpoint_path="/v1/chat/completions",
            )
        )
    assert exc.value.status_code == 502


def test_proxy_request_agent_typed_without_target_agent_id_is_clean_http_error(
    monkeypatch,
):
    """F1: agent-typed registry row without target_agent_id → 400 zh-TW, not 500.

    Reachable when AUTO_REGISTER_MODELS seeds model_type='agent' but no
    approved agents row matches — proxy_request is called without
    target_agent_id. Must not raise bare ValueError (unhandled → 500).
    """
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "agent-box")
    monkeypatch.setenv("ANILA_ALLOW_HTTP_AGENT_ENDPOINT", "1")
    model = SimpleNamespace(
        endpoint_url="http://agent-box:9100",
        model_type="agent",
        api_version="v1",
        name="orphan-agent-model",
        id=99,
        display_name=None,
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            proxy_request(
                model=model,
                api_key_id=1,
                user_id=1,
                department_id=None,
                request_body={"messages": []},
                endpoint_path="/v1/chat/completions",
                target_agent_id=None,
            )
        )
    assert exc.value.status_code == 400
    detail = str(exc.value.detail)
    assert "agent" in detail.lower() or "Agent" in detail
    assert "無法簽署" in detail
    assert exc.value.status_code != 500
