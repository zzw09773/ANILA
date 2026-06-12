"""Server endpoint shape — uses an injected mock pipeline so we never
load real 80GB weights in CI/dev.
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


def test_health_ok():
    client, _ = _make_client()
    r = client.get("/health")
    assert r.status_code == 200


def test_generate_returns_json_with_images():
    # Stage 1 (spec 3.2): /generate now returns JSON {images, seed, meta},
    # not raw image/png — even for N=1, always a list.
    client, captured = _make_client()
    r = client.post(
        "/generate", json={"prompt": "test", "aspect_ratio": "1:1", "seed": 42}
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    data = r.json()
    assert isinstance(data["images"], list) and len(data["images"]) == 1
    img = Image.open(BytesIO(base64.b64decode(data["images"][0])))
    assert img.format == "PNG"
    assert data["seed"] == 42
    assert "steps" in data["meta"] and "guidance" in data["meta"]


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


def test_generate_multiple_candidates():
    # Stage 1 returns list[1]; Stage 2 wants N — verify N candidates now.
    client, captured = _make_client()
    r = client.post(
        "/generate",
        json={"prompt": "x", "aspect_ratio": "1:1", "seed": 7, "num_candidates": 2},
    )
    assert r.status_code == 200
    assert len(r.json()["images"]) == 2
    assert len(captured) == 2  # pipeline invoked once per candidate


def test_generate_echoes_explicit_seed():
    # Explicit seed must round-trip for deterministic re-generation + audit.
    client, _ = _make_client()
    r = client.post(
        "/generate", json={"prompt": "x", "aspect_ratio": "1:1", "seed": 12345}
    )
    assert r.json()["seed"] == 12345


def test_generate_supports_section_band_3to1():
    # 3:1 letterbox (1536x512) added for SECTION_BAND use case.
    client, captured = _make_client()
    r = client.post("/generate", json={"prompt": "x", "aspect_ratio": "3:1", "seed": 1})
    assert r.status_code == 200
    band = captured[0]
    assert band["width"] > band["height"] * 2  # letterbox
