"""Fixtures for the router service's first test suite.

Two things have to be pinned before ``main`` is imported, or the answer
depends on where you were standing when you ran pytest:

1. ``anila_core.config.settings`` resolves ``env_file=".env"`` relative to
   cwd. Run from the repo root and it reads the developer box's real
   ``.env``, whose extra keys make the model raise ``extra_forbidden``.
   The import below therefore happens from a throwaway directory.
2. ``main`` reads ``CSP_BASE_URL`` / ``CSP_SERVICE_TOKEN`` at import time.
   They are pinned here so no test depends on the ambient environment.

The synchronous Starlette ``TestClient`` is not used here: this environment's
portal-backed client does not complete even a minimal ASGI request. The async
transport exercises the same app without that client-side deadlock.
"""

from __future__ import annotations

import os
import tempfile

import httpx
import pytest
import pytest_asyncio

os.environ.setdefault("CSP_BASE_URL", "http://csp:8000")
os.environ.setdefault("CSP_SERVICE_TOKEN", "pytest-fixed-not-a-real-token")
os.environ["ANILA_ROUTER_STATE_DIR"] = tempfile.mkdtemp(prefix="pytest-router-")

_cwd = os.getcwd()
os.chdir(tempfile.mkdtemp(prefix="pytest-router-cwd-"))
try:
    import main as router_main
finally:
    os.chdir(_cwd)


@pytest.fixture
def no_primary(monkeypatch):
    """CSP reports no primary routing model — the gate must fire."""

    async def _ensure_primary():
        return None, "測試：未設定主路由"

    monkeypatch.setattr(router_main, "_ensure_primary", _ensure_primary)


@pytest.fixture
def with_primary(monkeypatch):
    """CSP reports a primary routing model — the gate must not fire."""

    async def _ensure_primary():
        return "google/gemma4", None

    monkeypatch.setattr(router_main, "_ensure_primary", _ensure_primary)


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=router_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
