"""對話模型不可用時，Router 以 anila.error 帶出原因，不改叫另一個模型。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import respx
from fastapi.testclient import TestClient

from anila_core.api.router_server import create_router_app
from anila_core.config import settings
from anila_core.memory import close_all_connections

CSP_BASE = settings.csp_base_url.rstrip("/")
CSP_RESOLVE_URL = f"{CSP_BASE}/api/router-models/resolve"
CSP_CHAT_URL = f"{CSP_BASE}/v1/chat/completions"
CSP_AGENTS_URL = f"{CSP_BASE}/v1/agents"

UNAVAILABLE = {
    "code": "model_unavailable",
    "reason": "inactive",
    "display_name": "glm-5.3-flash",
    "name": "glm-5.3-flash",
}


@pytest_asyncio.fixture
async def db_path(tmp_path: Path):
    db = tmp_path / "router-model-unavailable.db"
    yield db
    await close_all_connections()


def _events(body: str) -> list[tuple[str, dict]]:
    found = []
    for block in body.split("\n\n"):
        event = "message"
        data = ""
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data += line.split(":", 1)[1].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            found.append((event, payload))
    return found


@respx.mock
@pytest.mark.real_router_model_resolve
def test_stream_unavailable_model_is_anila_error_without_fallback(db_path: Path):
    respx.post(CSP_RESOLVE_URL).mock(
        return_value=httpx.Response(409, json={"detail": UNAVAILABLE})
    )
    chat = respx.post(CSP_CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "不該出現的回答"}}
                ]
            },
        )
    )
    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test"},
        json={
            "model": "anila-router",
            "stream": True,
            "messages": [{"role": "user", "content": "這題還在"}],
        },
    )
    assert resp.status_code == 200, resp.text
    errors = [payload for name, payload in _events(resp.text) if name == "anila.error"]
    assert errors, resp.text
    assert errors[-1]["code"] == "model_unavailable"
    assert errors[-1]["reason"] == "inactive"
    assert errors[-1]["display_name"] == "glm-5.3-flash"
    assert chat.called is False
    assert "不該出現的回答" not in resp.text


@respx.mock
@pytest.mark.real_router_model_resolve
def test_nonstream_unavailable_model_is_not_answered_by_another_model(db_path: Path):
    respx.post(CSP_RESOLVE_URL).mock(
        return_value=httpx.Response(
            409,
            json={
                "detail": {
                    **UNAVAILABLE,
                    "reason": "not_granted",
                }
            },
        )
    )
    chat = respx.post(CSP_CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "fallback"}}]},
        )
    )
    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    client = TestClient(create_router_app(session_db_path=str(db_path)))
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test"},
        json={
            "model": "anila-router",
            "stream": False,
            "messages": [{"role": "user", "content": "這題還在"}],
        },
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "model_unavailable"
    assert detail["reason"] == "not_granted"
    assert chat.called is False
