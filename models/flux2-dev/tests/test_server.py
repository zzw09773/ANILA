"""Server endpoint shape — uses an injected mock pipeline so we never
load real 80GB weights in CI/dev.
"""
from __future__ import annotations

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


def test_health_ok():
    client, _ = _make_client()
    r = client.get("/health")
    assert r.status_code == 200


def test_generate_returns_png():
    client, captured = _make_client()
    r = client.post("/generate", json={"prompt": "test", "aspect_ratio": "1:1"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    img = Image.open(BytesIO(r.content))
    assert img.format == "PNG"


def test_generate_passes_prompt_to_pipeline():
    client, captured = _make_client()
    client.post("/generate", json={"prompt": "a tank", "aspect_ratio": "16:9"})
    assert len(captured) == 1
    assert captured[0]["prompt"] == "a tank"


def test_generate_translates_aspect_ratio_to_dimensions():
    client, captured = _make_client()

    client.post("/generate", json={"prompt": "x", "aspect_ratio": "1:1"})
    client.post("/generate", json={"prompt": "x", "aspect_ratio": "16:9"})
    client.post("/generate", json={"prompt": "x", "aspect_ratio": "9:16"})

    sq, wide, tall = captured
    assert sq["width"] == sq["height"]
    assert wide["width"] > wide["height"]
    assert tall["height"] > tall["width"]


def test_generate_rejects_unknown_aspect_ratio():
    client, _ = _make_client()
    r = client.post("/generate", json={"prompt": "x", "aspect_ratio": "47:11"})
    assert r.status_code == 422
