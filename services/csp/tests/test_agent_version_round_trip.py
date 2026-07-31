# -*- coding: utf-8 -*-
"""註冊時填的 agent 版本必須真的存下來,而且讀得回來。

原本的失效形狀(靜默成功)
------------------------
- 治理 UI 送 ``version``。
- ``AgentRegisterRequest`` 沒有這個欄位,而 pydantic BaseModel 預設
  ``extra="ignore"`` —— 欄位被收下然後丟掉,沒有任何錯誤。
- 回應與 DB 欄位叫 ``agent_version``(models/agent.py:83),永遠是 NULL,
  所以詳情頁的「版本」永遠顯示「—」。

開發者填了版本、按下註冊、看到綠色成功訊息,而那個值從來沒有存在過。

本檔釘住:欄位名以資料庫欄位 ``agent_version`` 為準,舊拼法 ``version``
(治理 UI 舊版與 packages/anila-core CLI 都送這個名字)以別名接受,
manifest 的 ``version`` 在未明送時作為來源。
"""

from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest

from app.models.agent import Agent
from tests.conftest import login, make_model, make_user

pytestmark = pytest.mark.filterwarnings("ignore")


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _env_allowances(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "agent,mock-llm")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-" + "a" * 40)


def _register(client, token, model, name, extra: dict) -> dict:
    payload = {
        "name": name,
        "endpoint_url": "http://agent:9100",
        "description_for_router": "x",
        "base_model_id": model.id,
    }
    payload.update(extra)
    resp = client.post("/api/agents/register", headers=_bearer(token), json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_agent_version_survives_register_and_read(client, db):
    """送出 → 存進 DB → 註冊回應 → 之後 GET 都拿得到同一個值。"""
    make_user(db, username="ver_dev", role="developer")
    model = make_model(db, name="ver-model")
    token = login(client, "ver_dev")

    created = _register(client, token, model, "ver-agent", {"agent_version": "1.2.3"})
    assert created["agent_version"] == "1.2.3", "註冊回應就已經把值丟了"

    # 真的落到資料庫欄位,不是只在回應裡回聲。
    row = db.query(Agent).filter(Agent.name == "ver-agent").first()
    assert row is not None
    assert row.agent_version == "1.2.3"

    # 再讀一次(詳情頁走的路徑)。
    detail = client.get(f"/api/agents/{created['id']}", headers=_bearer(token))
    assert detail.status_code == 200, detail.text
    assert detail.json()["agent_version"] == "1.2.3"

    # 列表(治理 UI 表格那一欄)。
    listing = client.get("/api/agents", headers=_bearer(token))
    assert listing.status_code == 200, listing.text
    row_json = next(a for a in listing.json() if a["id"] == created["id"])
    assert row_json["agent_version"] == "1.2.3"


def test_legacy_version_key_is_still_stored_not_dropped(client, db):
    """舊拼法 ``version``(anila-core CLI 仍在送)必須存進 agent_version。"""
    make_user(db, username="ver_dev2", role="developer")
    model = make_model(db, name="ver-model2")
    token = login(client, "ver_dev2")

    created = _register(client, token, model, "ver-agent2", {"version": "2.0.0"})
    assert created["agent_version"] == "2.0.0"
    row = db.query(Agent).filter(Agent.name == "ver-agent2").first()
    assert row.agent_version == "2.0.0"


def test_manifest_version_used_when_no_explicit_version(client, db):
    """manifest 的 version 欄位對映同一個 DB 欄位(models/agent.py:82 註解)。"""
    make_user(db, username="ver_dev3", role="developer")
    model = make_model(db, name="ver-model3")
    token = login(client, "ver_dev3")

    manifest = {
        "agent_id": "ver-agent3",
        "name": "版本 agent",
        "version": "3.1.4",
        "runtime_type": "langchain",
        "api_version": "v1",
        "supported_task_types": ["analyze"],
        "description_for_router": "x",
        "capabilities": {"retrieval": False, "tools": [], "streaming": False},
        "trace": {
            "required": True,
            "protocol": "anila-full-trace-v1",
            "callback_mode": "sse_and_post",
        },
        "classification": {"ceiling": "機密", "default": "營業秘密"},
    }
    created = _register(
        client, token, model, "ver-agent3",
        {"runtime_type": "langchain", "manifest": manifest},
    )
    assert created["agent_version"] == "3.1.4"


def test_explicit_version_wins_over_manifest(client, db):
    make_user(db, username="ver_dev4", role="developer")
    model = make_model(db, name="ver-model4")
    token = login(client, "ver_dev4")

    manifest = {
        "agent_id": "ver-agent4",
        "name": "版本 agent",
        "version": "0.0.1",
        "runtime_type": "langchain",
        "api_version": "v1",
        "supported_task_types": ["analyze"],
        "description_for_router": "x",
        "capabilities": {"retrieval": False, "tools": [], "streaming": False},
        "trace": {
            "required": True,
            "protocol": "anila-full-trace-v1",
            "callback_mode": "sse_and_post",
        },
        "classification": {"ceiling": "機密", "default": "營業秘密"},
    }
    created = _register(
        client, token, model, "ver-agent4",
        {"runtime_type": "langchain", "manifest": manifest, "agent_version": "9.9.9"},
    )
    assert created["agent_version"] == "9.9.9"


def test_no_version_means_null_not_empty_string(client, db):
    make_user(db, username="ver_dev5", role="developer")
    model = make_model(db, name="ver-model5")
    token = login(client, "ver_dev5")

    created = _register(client, token, model, "ver-agent5", {})
    assert created["agent_version"] is None
