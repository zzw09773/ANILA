"""POST /v1/images/generations — OpenAI Images API 相容端點。

雲端算力中心的 FLUX 服務改走標準 OpenAI Images API;本機 dev 後端新增
同款端點對齊新契約,方便本機開發與測試。既有 /generate 保留不動
(向後相容)。生成邏輯與 /generate 共用,測試注入 mock pipeline。
"""
from __future__ import annotations

import base64
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from server import build_app


class _MockPipeline:
    """Stand-in for the real diffusers Flux2Pipeline."""

    def __init__(self, captured: list[dict]):
        self._captured = captured

    def __call__(self, **kwargs):
        self._captured.append(kwargs)
        img = Image.new("RGB", (kwargs["width"], kwargs["height"]), color=(10, 20, 30))

        class _Out:
            images = [img]

        return _Out()


def _make_client() -> tuple[TestClient, list[dict]]:
    captured: list[dict] = []
    app = build_app(pipeline=_MockPipeline(captured))
    return TestClient(app), captured


def test_openai_endpoint_returns_created_and_b64_json_data():
    client, _ = _make_client()
    r = client.post(
        "/v1/images/generations",
        json={
            "model": "flux.2-dev",
            "prompt": "a tank",
            "n": 1,
            "size": "1024x1024",
            "response_format": "b64_json",
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data["created"], int)
    assert isinstance(data["data"], list) and len(data["data"]) == 1
    img = Image.open(BytesIO(base64.b64decode(data["data"][0]["b64_json"])))
    assert img.format == "PNG"
    assert img.size == (1024, 1024)


def test_openai_endpoint_maps_size_to_width_height():
    client, captured = _make_client()
    r = client.post(
        "/v1/images/generations",
        json={"prompt": "x", "size": "1792x1024"},
    )
    assert r.status_code == 200
    assert captured[0]["width"] == 1792
    assert captured[0]["height"] == 1024


def test_openai_endpoint_n_candidates():
    client, captured = _make_client()
    r = client.post(
        "/v1/images/generations",
        json={"prompt": "x", "n": 2, "size": "1024x1024"},
    )
    assert r.status_code == 200
    assert len(r.json()["data"]) == 2
    assert len(captured) == 2  # pipeline invoked once per candidate


def test_openai_endpoint_defaults():
    # 未帶 n / size / response_format → n=1、1024x1024、b64_json
    client, captured = _make_client()
    r = client.post("/v1/images/generations", json={"prompt": "defaults"})
    assert r.status_code == 200
    assert len(r.json()["data"]) == 1
    assert captured[0]["width"] == 1024
    assert captured[0]["height"] == 1024
    assert captured[0]["prompt"] == "defaults"


def test_openai_endpoint_rejects_url_response_format():
    # 本服務只出 b64_json(不架靜態檔案伺服器)→ url 格式回 400。
    client, _ = _make_client()
    r = client.post(
        "/v1/images/generations",
        json={"prompt": "x", "response_format": "url"},
    )
    assert r.status_code == 400


def test_openai_endpoint_rejects_malformed_size():
    client, _ = _make_client()
    r = client.post(
        "/v1/images/generations",
        json={"prompt": "x", "size": "not-a-size"},
    )
    assert r.status_code == 422


def test_legacy_generate_endpoint_still_works():
    # 向後相容:既有 /generate 合約不動。
    client, _ = _make_client()
    r = client.post(
        "/generate", json={"prompt": "legacy", "aspect_ratio": "1:1", "seed": 3}
    )
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data["images"], list) and len(data["images"]) == 1
    assert data["seed"] == 3
