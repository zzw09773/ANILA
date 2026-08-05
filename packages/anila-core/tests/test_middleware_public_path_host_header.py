"""The public-path checks must read the path the router dispatches on.

``CspServiceTokenMiddleware``, ``RotatingServiceTokenMiddleware`` and
``DispatchIdentityMiddleware`` all decide "is this a public path?" before
touching any credential. That decision used to read ``request.url.path``,
which starlette < 1.0.1 assembles by concatenating the caller's raw
``Host`` header with the path (CVE-2026-48710) — so the caller could
change what the middleware believed the path was while the router kept
dispatching on ``scope["path"]``.

Exact-set membership made the divergence fail *closed* here (a polluted
Host turns ``/health`` into ``/x/health`` and demands a token rather than
skipping one), which is why this is a hardening test rather than a
vulnerability regression. The invariant being pinned is the one that
matters: middleware and router read the same string.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import URL
from starlette.requests import HTTPConnection

from anila_core.api.middleware.auth import (
    CspServiceTokenMiddleware,
    RotatingServiceTokenMiddleware,
)
from anila_core.api.middleware.dispatch_auth import DispatchIdentityMiddleware

# Host headers carrying a path segment. Under the old code each of these
# made ``request.url.path`` differ from the routed path.
POLLUTED_HOSTS = [
    "attacker.example/health",
    "attacker.example/docs",
    "attacker.example/openapi.json",
    "attacker.example/redoc",
]


def _app_with(mw_class, **kwargs) -> FastAPI:
    app = FastAPI()
    app.add_middleware(mw_class, **kwargs)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/private")
    def private():
        return {"secret": "reached"}

    return app


@pytest.fixture(
    params=[
        pytest.param(
            (CspServiceTokenMiddleware, {"service_token": "tok", "dev_mode": False}),
            id="static",
        ),
        pytest.param(
            (RotatingServiceTokenMiddleware, {"env_token": "tok", "dev_mode": False}),
            id="rotating",
        ),
        pytest.param(
            (DispatchIdentityMiddleware, {"dev_mode": False}),
            id="dispatch-identity",
        ),
    ]
)
def client(request, tmp_path) -> TestClient:
    mw_class, kwargs = request.param
    if mw_class is RotatingServiceTokenMiddleware:
        kwargs = {**kwargs, "state_dir": tmp_path}
    return TestClient(_app_with(mw_class, **kwargs))


@pytest.mark.parametrize("host", POLLUTED_HOSTS)
def test_public_path_verdict_ignores_the_host_header(client, host):
    """``/health`` stays public no matter what the Host header claims."""
    resp = client.get("/health", headers={"Host": host})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok"}


@pytest.mark.parametrize("host", POLLUTED_HOSTS)
def test_protected_path_stays_protected(client, host):
    """And a Host header cannot make a protected path look public."""
    resp = client.get("/private", headers={"Host": host})
    assert resp.status_code in (401, 403), resp.text
    assert "secret" not in resp.text


# ---------------------------------------------------------------------------
# Version-independent twins.
#
# On starlette >= 1.0.1 no Host header can make ``request.url.path`` differ
# from ``scope["path"]``, so the two tests above pass even with the fix
# reverted. These substitute the **public** ``url`` property — where starlette
# defines it, on both 0.49.3 and 1.3.1 — to stand in for any future library
# regression, so reverting stays visible on the version we actually install.
# ---------------------------------------------------------------------------


@pytest.fixture
def url_reports_a_public_path(monkeypatch):
    """``request.url`` always claims ``/health``, whatever the routed path is."""
    monkeypatch.setattr(
        HTTPConnection, "url", property(lambda self: URL("http://attacker.example/health"))
    )


@pytest.fixture
def url_reports_a_private_path(monkeypatch):
    """``request.url`` prefixes the routed path so it never matches the set."""
    monkeypatch.setattr(
        HTTPConnection,
        "url",
        property(lambda self: URL(f"http://attacker.example/x{self.scope['path']}")),
    )


def test_protected_path_holds_through_a_library_regression(
    client, url_reports_a_public_path
):
    """The stronger direction: a substituted URL must not open ``/private``."""
    resp = client.get("/private")
    assert resp.status_code in (401, 403), resp.text
    assert "secret" not in resp.text


def test_public_path_holds_through_a_library_regression(
    client, url_reports_a_private_path
):
    resp = client.get("/health")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok"}
