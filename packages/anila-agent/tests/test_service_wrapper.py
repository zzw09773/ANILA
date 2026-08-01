"""service-wrapper 端點契約（in-process TestClient，不連網）。

只測不需模型的路徑：health 探針、/v1/models manifest（model_type=agent）、
以及無 Authorization 時 /v1/chat/completions 的 401 fail-closed。
"""

from __future__ import annotations

import importlib

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit


async def _fake_claims(_authorization=None, **_kwargs):
    return {"user_id": 1, "department": None, "agent_id": 9}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("ANILA_ALLOW_NO_SERVICE_TOKEN", raising=False)
    from anila_agent.serving import auth as auth_mod
    from anila_agent.serving import service_wrapper

    auth_mod.reset_jwks_client_for_tests()
    importlib.reload(service_wrapper)
    with TestClient(service_wrapper.app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_models_manifest_marks_agent(client):
    r = client.get("/v1/models")
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "list"
    assert data["data"][0]["model_type"] == "agent"  # CSP 註冊標記


def test_chat_without_token_is_401(client):
    r = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 401  # fail-closed：缺 Bearer JWT


def test_chat_503_when_collection_id_unset(monkeypatch):
    # 認證放行（mock 驗章）但缺 ANILA_COLLECTION_ID → 明確 503。
    monkeypatch.delenv("ANILA_COLLECTION_ID", raising=False)
    from anila_agent.serving import auth as auth_mod
    from anila_agent.serving import service_wrapper

    auth_mod.reset_jwks_client_for_tests()
    importlib.reload(service_wrapper)
    monkeypatch.setattr(
        service_wrapper, "verify_dispatch_authorization", _fake_claims
    )
    monkeypatch.setattr(service_wrapper, "COLLECTION_ID", 0)
    with TestClient(service_wrapper.app) as c:
        r = c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test"},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 503
