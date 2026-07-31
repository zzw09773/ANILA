"""Audit-log source IP must be what the proxy observed, not what the
caller claimed via a forged ``X-Forwarded-For`` first hop.

nginx (the only proxy in this deployment) sets ``X-Real-IP`` to
``$remote_addr`` and appends the real client to XFF via
``proxy_add_x_forwarded_for``. The four former helpers all took the
*first* XFF hop; they now re-export the shared ``client_ip``.
"""
from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from starlette.requests import Request

from app.api.agents import _common as agents_common
from app.api import banners as banners_mod
from app.api import models as models_mod
from app.api import service_clients as service_clients_mod
from app.utils.client_ip import client_ip


# Documentation / RFC 5737 TEST-NET addresses only — never real hosts.
FORGED_CLIENT = "198.51.100.77"
OBSERVED_BY_PROXY = "203.0.113.50"
SOCKET_PEER = "192.0.2.10"
PROXY_SOCKET = "192.0.2.2"


def _request(
    *,
    headers: list[tuple[str, str]] | None = None,
    client_host: str | None = SOCKET_PEER,
) -> Request:
    header_list = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or [])
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": header_list,
        "client": (client_host, 54321) if client_host is not None else None,
        "server": ("testserver", 80),
    }
    return Request(scope)


# Every former copy — identity with the shared helper so a local
# reintroduction of the first-hop bug cannot hide in one module.
_HELPER_IMPORTS = [
    ("app.utils.client_ip.client_ip", client_ip),
    ("app.api.agents._common._client_ip", agents_common._client_ip),
    ("app.api.banners._client_ip", banners_mod._client_ip),
    ("app.api.models._client_ip", models_mod._client_ip),
    ("app.api.service_clients._client_ip", service_clients_mod._client_ip),
]


@pytest.mark.parametrize("name,helper", _HELPER_IMPORTS)
def test_each_helper_is_the_shared_client_ip(name, helper):
    assert helper is client_ip, f"{name} is not the shared client_ip"


@pytest.mark.parametrize("name,helper", _HELPER_IMPORTS)
def test_forged_xff_records_proxy_observed_address(name, helper):
    """Acceptance 1: forged first hop must not win.

    Simulates nginx: X-Real-IP = observed; XFF = forged,observed;
    socket peer = the proxy itself.
    """
    req = _request(
        headers=[
            ("x-forwarded-for", f"{FORGED_CLIENT}, {OBSERVED_BY_PROXY}"),
            ("x-real-ip", OBSERVED_BY_PROXY),
        ],
        client_host=PROXY_SOCKET,
    )
    assert helper(req) == OBSERVED_BY_PROXY, name


@pytest.mark.parametrize("name,helper", _HELPER_IMPORTS)
def test_xff_without_real_ip_uses_last_hop(name, helper):
    """When only XFF is present, take the hop the proxy appended."""
    req = _request(
        headers=[
            ("x-forwarded-for", f"{FORGED_CLIENT}, {OBSERVED_BY_PROXY}"),
        ],
        client_host=PROXY_SOCKET,
    )
    assert helper(req) == OBSERVED_BY_PROXY, name


@pytest.mark.parametrize("name,helper", _HELPER_IMPORTS)
def test_no_proxy_falls_back_to_socket_peer(name, helper):
    """Acceptance 2: direct access / tests — socket peer, not null/fake."""
    req = _request(headers=[], client_host=SOCKET_PEER)
    assert helper(req) == SOCKET_PEER, name
    assert helper(req) is not None, name


@pytest.mark.parametrize("name,helper", _HELPER_IMPORTS)
def test_none_request_returns_none(name, helper):
    assert helper(None) is None, name
