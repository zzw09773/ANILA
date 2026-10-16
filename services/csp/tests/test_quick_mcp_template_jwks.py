"""範本 JWKS 連線綁定；僅使用合成 DNS 與記憶體 HTTP transport。"""
from __future__ import annotations

import asyncio
import importlib.util
import ipaddress
from pathlib import Path
import socket
import sys

import httpx
import pytest


@pytest.fixture
def template():
    path = Path(__file__).resolve().parents[3] / "examples/quick-builder-mcp/server.py"
    spec = importlib.util.spec_from_file_location("quick_mcp_template_jwks", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def install_transport(template, monkeypatch, handler):
    client_type = httpx.AsyncClient
    options = []

    def client(**kwargs):
        options.append(kwargs.copy())
        return client_type(transport=httpx.MockTransport(handler), **kwargs)

    async def inline(function, *args):
        return function(*args)

    monkeypatch.setattr(template.httpx, "AsyncClient", client)
    monkeypatch.setattr(template, "run_in_threadpool", inline)
    monkeypatch.setattr(template, "verify_jwt", lambda *args, **kwargs: {"sub": "7"})
    return options


def addresses(*values):
    return [
        (
            socket.AF_INET6 if ":" in value else socket.AF_INET,
            socket.SOCK_STREAM, 6, "", (value, 443),
        )
        for value in values
    ]


@pytest.mark.parametrize(
    "url,private,host,sni",
    [
        ("https://jwks.internal/keys", "10.2.3.4", "jwks.internal", "jwks.internal"),
        (
            "https://jwks.internal:8443/keys", "fd00::7",
            "jwks.internal:8443", "jwks.internal",
        ),
        ("http://[fd00::7]:8080/keys", "fd00::7", "[fd00::7]:8080", "fd00::7"),
    ],
)
def test_checked_address_is_bound_to_request(
    template, monkeypatch, url, private, host, sni
):
    dns_calls = []
    connections = []

    def dns(name, port, **kwargs):
        dns_calls.append((name, port))
        return addresses(private if len(dns_calls) == 1 else "203.0.113.9")

    def respond(request):
        # Model a transport resolving only hostnames; literal IPs bypass DNS.
        try:
            target = ipaddress.ip_address(request.url.host)
        except ValueError:
            target = ipaddress.ip_address(
                dns(request.url.host, request.url.port)[0][4][0]
            )
        connections.append(str(target))
        assert str(target) == private
        assert request.headers["host"] == host
        assert request.extensions["sni_hostname"] == sni
        assert request.url.path == "/keys"
        expected_port = 8443 if ":8443" in url else 8080 if ":8080" in url else 443
        assert (request.url.port or 443) == expected_port
        return httpx.Response(200, json={"keys": []})

    monkeypatch.setattr(template.socket, "getaddrinfo", dns)
    options = install_transport(template, monkeypatch, respond)
    verifier = template.Verifier(template.Settings(service_id=3, jwks_url=url))
    assert asyncio.run(verifier.verify("synthetic", template.TOOL_NAME)) == {"sub": "7"}
    assert connections == [private]
    assert len(dns_calls) == 1
    assert options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False
    assert "verify" not in options[0]  # HTTPX's default CA verification remains enabled.


def test_each_hostname_uses_a_fresh_client(template, monkeypatch):
    monkeypatch.setattr(
        template.socket, "getaddrinfo", lambda *a, **k: addresses("10.2.3.4")
    )
    identities = []

    def respond(request):
        identities.append((
            request.url.host, request.headers["host"],
            request.extensions["sni_hostname"],
        ))
        return httpx.Response(200, json={"keys": []})

    options = install_transport(template, monkeypatch, respond)
    for host in ("first.internal", "second.internal"):
        verifier = template.Verifier(
            template.Settings(service_id=3, jwks_url=f"https://{host}/keys")
        )
        assert asyncio.run(verifier.verify("synthetic", template.TOOL_NAME)) == {"sub": "7"}
    assert len(options) == 2
    assert identities == [
        ("10.2.3.4", "first.internal", "first.internal"),
        ("10.2.3.4", "second.internal", "second.internal"),
    ]


@pytest.mark.parametrize(
    "values", [(), ("203.0.113.9",), ("10.2.3.4", "203.0.113.9"), ("::1",)]
)
def test_public_mixed_and_empty_dns_are_rejected(template, monkeypatch, values):
    monkeypatch.setattr(
        template.socket, "getaddrinfo", lambda *a, **k: addresses(*values)
    )
    calls = []
    install_transport(template, monkeypatch, lambda request: calls.append(request))
    verifier = template.Verifier(
        template.Settings(service_id=3, jwks_url="https://jwks.internal/keys")
    )
    with pytest.raises(template.HTTPException) as error:
        asyncio.run(verifier.verify("synthetic", template.TOOL_NAME))
    assert error.value.status_code == 503
    assert error.value.detail["error_type"] == "identity_unavailable"
    assert calls == []


@pytest.mark.parametrize("failure", ["timeout", "transport", "dns", "status", "encoding", "json", "size"])
def test_jwks_failures_are_classified(template, monkeypatch, failure):
    def dns(*args, **kwargs):
        if failure == "dns":
            raise socket.gaierror("synthetic")
        return addresses("10.2.3.4")

    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic", request=request)
        if failure == "transport":
            raise httpx.ConnectError("synthetic", request=request)
        if failure == "status":
            return httpx.Response(302, headers={"location": "https://outside.invalid"})
        if failure == "encoding":
            return httpx.Response(200, headers={"content-encoding": "br"})
        if failure == "size":
            return httpx.Response(200, content=b"x" * (65536 + 1))
        return httpx.Response(200, content=b"invalid-json")

    monkeypatch.setattr(template.socket, "getaddrinfo", dns)
    install_transport(template, monkeypatch, respond)
    verifier = template.Verifier(
        template.Settings(service_id=3, jwks_url="https://jwks.internal/keys")
    )
    with pytest.raises(template.HTTPException) as error:
        asyncio.run(verifier.verify("synthetic", template.TOOL_NAME))
    assert error.value.status_code == 503
    assert error.value.detail["error_type"] == "identity_unavailable"
