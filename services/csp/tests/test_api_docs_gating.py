"""Swagger /docs + /openapi.json are always admin-gated (S-117).

The OpenAPI schema and Swagger UI leak the full API surface, so CSP registers
only custom routes protected by ``require_admin``.  Anonymous callers must
receive 401 rather than either document.

These routes need neither the DB nor the lifespan, so we drive a bare
TestClient (no ``with`` block) to avoid the lifespan startup that the shared
``client`` fixture performs.

The enabled path (openapi_url="/openapi.json" + a registered /docs route) is
verified out-of-band by toggling the env var and rebuilding the app, which is
not done here to avoid reloading the whole app module mid-suite.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

# No `with` → lifespan startup is not run; these routes are static.
_client = TestClient(app)


def test_openapi_schema_requires_authentication():
    assert _client.get("/openapi.json").status_code == 401


def test_swagger_ui_requires_authentication():
    assert _client.get("/docs").status_code == 401


def test_redoc_is_404_by_default():
    """Redoc was already disabled (redoc_url=None) and stays off."""
    assert _client.get("/redoc").status_code == 404
