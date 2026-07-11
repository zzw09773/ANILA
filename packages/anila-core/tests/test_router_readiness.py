"""Gate 0 readiness contract for production Full Trace wiring."""

from __future__ import annotations

from fastapi.testclient import TestClient

from anila_core.api.router_server import create_router_app


def test_production_readiness_fails_when_trace_endpoint_is_missing(monkeypatch):
    monkeypatch.setenv("ANILA_ENV", "production")
    monkeypatch.delenv("ANILA_TRACE_ENDPOINT", raising=False)
    client = TestClient(create_router_app())

    assert client.get("/health").status_code == 200
    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["trace_endpoint_configured"] is False


def test_production_readiness_accepts_configured_trace_endpoint(monkeypatch):
    monkeypatch.setenv("ANILA_ENV", "production")
    monkeypatch.setenv("ANILA_TRACE_ENDPOINT", "http://csp:8000")
    client = TestClient(create_router_app())

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["trace_endpoint_configured"] is True


def test_development_readiness_allows_trace_to_be_disabled(monkeypatch):
    monkeypatch.setenv("ANILA_ENV", "development")
    monkeypatch.delenv("ANILA_TRACE_ENDPOINT", raising=False)
    client = TestClient(create_router_app())

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["trace_endpoint_configured"] is False
