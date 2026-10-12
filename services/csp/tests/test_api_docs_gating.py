"""Swagger /docs + /openapi.json are admin-gated (not a dead ENABLE_API_DOCS flag).

The OpenAPI schema and Swagger UI leak the full API surface, so the built-in
public FastAPI routes stay off (``docs_url=None``, ``openapi_url=None``) and
custom routes require admin tier. Unauthenticated callers must not see 200.

These routes need neither the DB nor the lifespan, so we drive a bare
TestClient (no ``with`` block) to avoid the lifespan startup that the shared
``client`` fixture performs.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

# No `with` → lifespan startup is not run; these routes are static.
_client = TestClient(app)


@pytest.mark.parametrize(
    "get_arg",
    [
        # Unauthenticated callers do not get the schema (401/403, never 200).
        pytest.param("/openapi.json", id='openapi_schema_requires_auth'),
        # Unauthenticated callers do not get Swagger UI (401/403, never 200).
        pytest.param("/docs", id='swagger_ui_requires_auth'),
    ],
)
def test_openapi_schema_requires_auth_cases(get_arg):
    status = _client.get(get_arg).status_code
    assert status in (401, 403)


def test_redoc_is_404():
    """Redoc stays disabled (redoc_url=None)."""
    assert _client.get("/redoc").status_code == 404


def test_enable_api_docs_setting_is_gone():
    """Dead ENABLE_API_DOCS flag must not linger on Settings."""
    from app.config import settings

    assert not hasattr(settings, "ENABLE_API_DOCS")
