"""Gate 0 readiness contract for production Full Trace wiring."""

from __future__ import annotations

from fastapi.testclient import TestClient

from anila_core.api import router_server
from anila_core.api.router_server import create_router_app


_NAMED_TOKEN_ENVS = (
    "ANILA_CSP_REGISTRY_SERVICE_TOKEN",
    "ANILA_CSP_AGENT_SERVICE_TOKEN",
    "ANILA_CSP_INFERENCE_SERVICE_TOKEN",
)


def _clear_named_tokens(monkeypatch):
    for name in _NAMED_TOKEN_ENVS:
        monkeypatch.delenv(name, raising=False)


def _set_named_tokens(monkeypatch):
    _clear_named_tokens(monkeypatch)
    for name in _NAMED_TOKEN_ENVS:
        monkeypatch.setenv(name, f"csk-{name.rsplit('_', 3)[-3].lower()}-token")


def test_production_readiness_fails_when_trace_endpoint_is_missing(monkeypatch):
    monkeypatch.setenv("ANILA_ENV", "production")
    monkeypatch.delenv("ANILA_TRACE_ENDPOINT", raising=False)
    _clear_named_tokens(monkeypatch)
    client = TestClient(create_router_app())

    assert client.get("/health").status_code == 200
    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["trace_endpoint_configured"] is False


def test_production_readiness_accepts_configured_trace_endpoint(monkeypatch):
    monkeypatch.setenv("ANILA_ENV", "production")
    monkeypatch.setenv("ANILA_TRACE_ENDPOINT", "http://csp:8000")
    _set_named_tokens(monkeypatch)
    client = TestClient(create_router_app())

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["trace_endpoint_configured"] is True
    assert response.json()["missing_capabilities"] == []


def test_production_readiness_fails_when_one_named_token_is_missing(monkeypatch):
    monkeypatch.setenv("ANILA_ENV", "production")
    monkeypatch.setenv("ANILA_TRACE_ENDPOINT", "http://csp:8000")
    _set_named_tokens(monkeypatch)
    monkeypatch.delenv("ANILA_CSP_INFERENCE_SERVICE_TOKEN", raising=False)

    response = TestClient(create_router_app()).get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["inference_service_token_configured"] is False
    assert "inference_service_token" in response.json()["missing_capabilities"]


def test_production_readiness_requires_explicit_injected_grant_minter_capability(monkeypatch):
    monkeypatch.setenv("ANILA_ENV", "production")
    monkeypatch.setenv("ANILA_TRACE_ENDPOINT", "http://csp:8000")
    _set_named_tokens(monkeypatch)
    # An injected transport is not trusted merely because it has a ``mint``
    # method; it must expose an explicit ``is_configured`` capability marker.
    response = TestClient(create_router_app(grant_minter=object())).get("/ready")

    assert response.status_code == 503
    assert response.json()["grant_minter_configured"] is False
    assert "grant_minter" in response.json()["missing_capabilities"]


def test_production_readiness_ignores_legacy_fleet_token(monkeypatch):
    monkeypatch.setenv("ANILA_ENV", "production")
    monkeypatch.setenv("ANILA_TRACE_ENDPOINT", "http://csp:8000")
    _clear_named_tokens(monkeypatch)
    monkeypatch.setattr(router_server.settings, "csp_service_token", "csk-legacy-fleet")

    response = TestClient(create_router_app()).get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["registry_service_token_configured"] is False
    assert response.json()["agent_service_token_configured"] is False
    assert response.json()["inference_service_token_configured"] is False


def test_development_readiness_allows_trace_to_be_disabled(monkeypatch):
    monkeypatch.setenv("ANILA_ENV", "development")
    monkeypatch.delenv("ANILA_TRACE_ENDPOINT", raising=False)
    _clear_named_tokens(monkeypatch)
    client = TestClient(create_router_app())

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["trace_endpoint_configured"] is False
