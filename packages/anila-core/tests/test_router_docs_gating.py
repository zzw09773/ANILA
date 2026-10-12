"""P2.3 — Router OpenAPI / docs surfaces must not be anonymously readable.

``create_router_app`` disables FastAPI's default ``/docs``, ``/redoc``, and
``/openapi.json`` (same disable mechanism as CSP). No admin-gated re-add:
the Router has no User/require_admin dependency. ``/health`` and ``/v1/models``
stay public for compose healthchecks and model listing.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from anila_core.api.router_server import create_router_app


def _client() -> TestClient:
    # No lifespan side effects needed for static route presence checks.
    return TestClient(create_router_app())


@pytest.mark.parametrize(
    "get_arg",
    [
        pytest.param("/openapi.json", id='router_openapi_schema_is_404_unauthenticated'),
        pytest.param("/docs", id='router_swagger_ui_is_404_unauthenticated'),
        pytest.param("/redoc", id='router_redoc_is_404_unauthenticated'),
    ],
)
def test_router_openapi_schema_is_404_unauthenticated_cases(get_arg) -> None:
    r = _client().get(get_arg)
    assert r.status_code == 404


def test_router_health_unaffected() -> None:
    r = _client().get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "cached_agents" in body


def test_router_v1_models_unaffected() -> None:
    r = _client().get("/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert body["data"][0]["id"] == "anila-router"
