"""FastAPI app integration test — exercises /health and
/v1/chat/completions with all collaborators stubbed.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.image_store import ImageStore
from app.main import build_app


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    translator = AsyncMock()
    translator.translate.return_value = "english prompt"

    flux_client = AsyncMock()
    flux_client.generate.return_value = _PNG

    class _Ctx:
        async def __aenter__(self):
            return flux_client

        async def __aexit__(self, *exc):
            return None

    app = build_app(
        translator=translator,
        flux_client_factory=lambda: _Ctx(),
        image_store=ImageStore(local_dir=tmp_path, public_url_prefix="/uploads/flux"),
        default_aspect_ratio="16:9",
    )
    return TestClient(app)


def test_health_returns_200(client: TestClient):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_chat_completions_returns_openai_shape(client: TestClient):
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "image-generator",
            "messages": [{"role": "user", "content": "畫一張坦克"}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert "![](" in body["choices"][0]["message"]["content"]


def test_chat_completions_rejects_empty_messages(client: TestClient):
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "image-generator", "messages": []},
    )
    assert resp.status_code == 422


def test_models_endpoint_lists_image_generator(client: TestClient):
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    body = resp.json()
    ids = [m["id"] for m in body["data"]]
    assert "image-generator" in ids
