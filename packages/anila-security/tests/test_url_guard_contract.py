from __future__ import annotations

import logging
import socket

import pytest

from anila_security import (
    ENDPOINT_KIND_AGENT,
    ENDPOINT_KIND_MODEL,
    REASON_DENY_HOST,
    REASON_PRIVATE_IP,
    REASON_SCHEME,
    UnsafeEndpointError,
    clear_trusted_host_providers,
    register_trusted_host_provider,
    validate_outbound_url,
)


@pytest.fixture(autouse=True)
def _isolated_guard(monkeypatch):
    for name in (
        "ANILA_ALLOW_HTTP_ENDPOINT",
        "ANILA_ALLOW_HTTP_AGENT_ENDPOINT",
        "ANILA_ALLOW_PRIVATE_ENDPOINT",
        "ANILA_TRUSTED_HOSTS",
        "ANILA_ENV",
    ):
        monkeypatch.delenv(name, raising=False)

    def public_dns(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", public_dns)
    clear_trusted_host_providers()
    yield
    clear_trusted_host_providers()


def test_production_model_http_is_rejected_even_when_global_flag_is_set(monkeypatch):
    monkeypatch.setenv("ANILA_ENV", "production")
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")

    with pytest.raises(UnsafeEndpointError) as exc:
        validate_outbound_url("http://api.example.com/v1", ENDPOINT_KIND_MODEL)

    assert exc.value.reason == REASON_SCHEME


def test_agent_http_has_domain_specific_opt_in(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_AGENT_ENDPOINT", "1")
    validate_outbound_url("http://api.example.com/v1", ENDPOINT_KIND_AGENT)


def test_legacy_agent_http_flag_warns(monkeypatch, caplog):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    with caplog.at_level(logging.WARNING, logger="anila_security.url_guard"):
        validate_outbound_url("http://api.example.com/v1", ENDPOINT_KIND_AGENT)
    assert "ANILA_ALLOW_HTTP_AGENT_ENDPOINT" in caplog.text


def test_trusted_provider_is_shared_but_cannot_bypass_structural_denies(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    register_trusted_host_provider(lambda: {"inference"})
    validate_outbound_url("http://inference:8000/v1")

    register_trusted_host_provider(lambda: {"localhost"})
    with pytest.raises(UnsafeEndpointError) as exc:
        validate_outbound_url("http://localhost:8000/v1")
    assert exc.value.reason == REASON_DENY_HOST


def test_private_ip_is_fail_closed_without_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    with pytest.raises(UnsafeEndpointError) as exc:
        validate_outbound_url("http://10.53.100.12:8000/v1")
    assert exc.value.reason == REASON_PRIVATE_IP


def test_provider_failure_does_not_disable_validation(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")

    def unavailable():
        raise RuntimeError("DB unavailable")

    register_trusted_host_provider(unavailable)
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url("http://not-trusted:8000/v1")
