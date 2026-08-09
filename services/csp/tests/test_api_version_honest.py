"""api_version is a URL path prefix — constrained, probe-consistent."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas.model_registry import ModelCreate, ModelUpdate
from app.services.proxy.service import ProxyTuning


def test_api_version_typo_rejected_on_create():
    with pytest.raises(ValidationError):
        ModelCreate(
            name="x",
            display_name="X",
            model_type="embedding",
            endpoint_url="https://example.com/v1",
            api_version="v3",
        )


def test_api_version_typo_rejected_on_update():
    with pytest.raises(ValidationError):
        ModelUpdate(api_version="V1")


def test_protocol_custom_adapter_rejected_by_schema():
    with pytest.raises(ValidationError):
        ModelCreate(
            name="x",
            display_name="X",
            model_type="embedding",
            endpoint_url="https://example.com/v1",
            protocol="custom_adapter",
        )


def test_protocol_triton_grpc_accepted_by_schema():
    m = ModelCreate(
        name="nv-embed-v2",
        display_name="NV Embed",
        model_type="embedding",
        endpoint_url="grpc://172.16.120.35:9001",
        protocol="triton_grpc",
    )
    assert m.protocol == "triton_grpc"


@pytest.mark.asyncio
async def test_probe_uses_api_version_path(monkeypatch):
    """Dim probe must agree with the call path on api_version."""
    from app.api import models as models_api
    import app.services.proxy.service as proxy_svc

    captured: dict = {}

    async def fake_proxy_request(**kwargs):
        captured.update(kwargs)
        return {"data": [{"embedding": [0.1] * 16}]}

    monkeypatch.setattr(proxy_svc, "proxy_request", fake_proxy_request)

    model = SimpleNamespace(
        name="embed",
        endpoint_url="http://mock-llm:8080/v1",
        api_version="v2",
        protocol="openai_compatible",
        model_type="embedding",
        id=1,
        api_key_secret_ref=None,
        display_name="embed",
        is_internal=False,
        classification_ceiling=None,
        is_active=True,
    )
    n = await models_api._probe_embedding_native_dim(model, ProxyTuning.from_registry_defaults())
    assert n == 16
    assert captured.get("endpoint_path") == "/v2/embeddings"
    assert captured.get("embedding_input_role") == "query"
