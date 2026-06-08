"""Swagger /docs + /openapi.json are gated behind ENABLE_API_DOCS (S-117).

The OpenAPI schema and Swagger UI are unauthenticated and leak the full API
surface, so they are OFF by default (secure-by-default) and only enabled in
dev via ENABLE_API_DOCS=true. The conftest does not set ENABLE_API_DOCS, so
the app under test is built with docs disabled — exactly the prod posture.

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


def test_openapi_schema_is_404_by_default():
    """Prod default: the OpenAPI schema route is disabled entirely."""
    assert _client.get("/openapi.json").status_code == 404


def test_swagger_ui_is_404_by_default():
    """Prod default: the custom Swagger UI route is not registered."""
    assert _client.get("/docs").status_code == 404


def test_redoc_is_404_by_default():
    """Redoc was already disabled (redoc_url=None) and stays off."""
    assert _client.get("/redoc").status_code == 404
