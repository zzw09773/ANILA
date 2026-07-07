"""Integration coverage for 「執行期向 csp 拉取 FLUX 端點/模型」(見
docs/superpowers/specs/2026-07-06-flux-image-primary-design.md §3):

  - csp 有值時優先於 env,且 60 秒內第二次生圖不重打 csp。
  - csp 404 → fallback env,且不炸。
  - TTL 過期後 csp 回新端點 → 下一次生圖打到新 URL(FluxClient 重建)。
  - service token 被拒(401)→ fallback env,不炸。

用 build_app() 組出真正的 ChatHandler + ImagePrimaryFetcher +
BackendResolver + FluxClient 工廠(非 mock),respx 同時 mock csp 的
image-primary 端點與 FLUX 的 images/generations 端點,驗證整條路徑。
TestClient 內部自己管事件迴圈,這裡不用 async def,直接同步呼叫。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import base64
import httpx
import respx
from fastapi.testclient import TestClient

from app.backend_resolver import BackendResolver
from app.flux_client import FluxClient
from app.image_primary_fetcher import ImagePrimaryFetcher
from app.image_store import ImageStore
from app.main import build_app

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
_B64 = base64.b64encode(_PNG).decode()
_IMAGES_BODY = {"created": 1_720_000_000, "data": [{"b64_json": _B64}]}

_CSP_URL = "http://csp:8000/api/models/image-primary"
_CSP_PRIMARY_A = {
    "id": 1,
    "name": "flux-cloud-a",
    "display_name": "FLUX Cloud A",
    "model_type": "image",
    "endpoint_url": "https://flux-a.example.com",
    "api_version": None,
    "health_status": "healthy",
}
_CSP_PRIMARY_B = {
    **_CSP_PRIMARY_A,
    "name": "flux-cloud-b",
    "endpoint_url": "https://flux-b.example.com",
}

_ENV_BACKEND_URL = "http://flux2-dev:8000"
_ENV_MODEL = "flux.2-dev"


def _build_client(tmp_path: Path, *, ttl_seconds: float = 60.0) -> TestClient:
    translator = AsyncMock()
    translator.translate.return_value = "english prompt"

    fetcher = ImagePrimaryFetcher(
        csp_base_url="http://csp:8000",
        service_token="svc-token",
        ttl_seconds=ttl_seconds,
        timeout=5.0,
    )
    resolver = BackendResolver(
        fetcher=fetcher,
        fallback_endpoint=_ENV_BACKEND_URL,
        fallback_model=_ENV_MODEL,
    )

    def flux_factory(endpoint: str, model: str) -> FluxClient:
        return FluxClient(base_url=endpoint, timeout=10.0, model=model, api_key="")

    app = build_app(
        translator=translator,
        flux_client_factory=flux_factory,
        image_store=ImageStore(local_dir=tmp_path, public_url_prefix="/uploads/flux"),
        backend_resolver=resolver,
        default_aspect_ratio="16:9",
    )
    return TestClient(app)


def _generate(client: TestClient) -> httpx.Response:
    return client.post(
        "/v1/chat/completions",
        json={"model": "image-generator", "messages": [{"role": "user", "content": "畫一張坦克"}]},
    )


@respx.mock
def test_csp_value_used_and_ttl_prevents_second_csp_call(tmp_path: Path):
    csp_route = respx.get(_CSP_URL).mock(return_value=httpx.Response(200, json=_CSP_PRIMARY_A))
    flux_route = respx.post("https://flux-a.example.com/v1/images/generations").mock(
        return_value=httpx.Response(200, json=_IMAGES_BODY)
    )

    client = _build_client(tmp_path, ttl_seconds=60.0)

    resp1 = _generate(client)
    resp2 = _generate(client)

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert csp_route.call_count == 1  # 60s TTL: 第二次生圖不重打 csp
    assert flux_route.call_count == 2
    body = json.loads(flux_route.calls.last.request.content)
    assert body["model"] == "flux-cloud-a"


@respx.mock
def test_csp_404_falls_back_to_env_backend(tmp_path: Path):
    csp_route = respx.get(_CSP_URL).mock(return_value=httpx.Response(404, json={"detail": "未設定"}))
    flux_route = respx.post(f"{_ENV_BACKEND_URL}/v1/images/generations").mock(
        return_value=httpx.Response(200, json=_IMAGES_BODY)
    )

    client = _build_client(tmp_path)
    resp = _generate(client)

    assert resp.status_code == 200
    assert csp_route.call_count == 1
    assert flux_route.called
    body = json.loads(flux_route.calls.last.request.content)
    assert body["model"] == _ENV_MODEL


@respx.mock
def test_endpoint_hot_swap_after_ttl_expiry(tmp_path: Path):
    csp_route = respx.get(_CSP_URL).mock(
        side_effect=[
            httpx.Response(200, json=_CSP_PRIMARY_A),
            httpx.Response(200, json=_CSP_PRIMARY_B),
        ]
    )
    flux_a = respx.post("https://flux-a.example.com/v1/images/generations").mock(
        return_value=httpx.Response(200, json=_IMAGES_BODY)
    )
    flux_b = respx.post("https://flux-b.example.com/v1/images/generations").mock(
        return_value=httpx.Response(200, json=_IMAGES_BODY)
    )

    client = _build_client(tmp_path, ttl_seconds=0.05)

    resp1 = _generate(client)
    time.sleep(0.1)
    resp2 = _generate(client)

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert csp_route.call_count == 2
    assert flux_a.call_count == 1
    assert flux_b.call_count == 1  # TTL 過期後打到新端點,FluxClient 已重建


@respx.mock
def test_csp_401_falls_back_to_env_without_crashing(tmp_path: Path):
    csp_route = respx.get(_CSP_URL).mock(return_value=httpx.Response(401, json={"detail": "無效"}))
    flux_route = respx.post(f"{_ENV_BACKEND_URL}/v1/images/generations").mock(
        return_value=httpx.Response(200, json=_IMAGES_BODY)
    )

    client = _build_client(tmp_path)
    resp = _generate(client)

    assert resp.status_code == 200
    assert csp_route.call_count == 1
    assert flux_route.called
