"""Health probe branches on protocol=triton_grpc."""
from __future__ import annotations

import asyncio

import pytest

from app.services import health_checker


@pytest.mark.asyncio
async def test_triton_protocol_uses_grpc_probe(monkeypatch):
    called: dict = {}

    def fake_probe(endpoint_url, *, model_name=None):
        called["url"] = endpoint_url
        called["model_name"] = model_name
        return "healthy", 12

    monkeypatch.setattr(
        "app.services.triton_grpc.probe_triton_health", fake_probe
    )
    # Also used via package import inside probe_model_health_detailed
    monkeypatch.setattr(
        "app.services.triton_grpc.client.probe_triton_health", fake_probe
    )

    status, ms = await health_checker.probe_model_health_detailed(
        "grpc://172.16.120.35:9001",
        endpoint_kind="model",
        protocol="triton_grpc",
        model_name="nv-embed-v2",
        skip_validate=True,
    )
    assert status == "healthy"
    assert ms == 12
    assert called["model_name"] == "nv-embed-v2"


@pytest.mark.asyncio
async def test_openai_protocol_still_http_probes(monkeypatch):
    """Non-triton must not call the gRPC probe."""
    monkeypatch.setattr(
        "app.services.triton_grpc.probe_triton_health",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no triton")),
    )

    async def fake_get(url):
        class R:
            status_code = 200

        return R()

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return await fake_get(url)

    monkeypatch.setattr(health_checker.httpx, "AsyncClient", FakeClient)
    status, _ = await health_checker.probe_model_health_detailed(
        "http://mock-llm:8080/v1",
        endpoint_kind="model",
        protocol="openai_compatible",
        skip_validate=True,
    )
    assert status == "healthy"
