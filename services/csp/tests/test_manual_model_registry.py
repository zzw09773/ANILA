"""模型只在治理中心登錄。開機不再讀 AUTO_REGISTER_*。

空的登錄表必須能起來。七個角色都還沒指定時，角色 API 的 status 是
unset，治理中心用這個狀態顯示「需設定」。
"""
from __future__ import annotations

import json

from app.models.agent import Agent
from app.models.model_registry import ModelRegistry
from app.services import auto_seed
from tests.conftest import login, make_user


class _KeepOpen:
    def __init__(self, session):
        self._session = session

    def close(self):
        return None

    def __getattr__(self, name):
        return getattr(self._session, name)


def test_auto_seed_does_not_register_models_or_agents_from_env(db, monkeypatch):
    monkeypatch.setattr(auto_seed, "SessionLocal", lambda: _KeepOpen(db))
    monkeypatch.setattr(auto_seed.settings, "AUTO_SEED_API_KEYS", "")
    payload_models = json.dumps(
        [
            {
                "name": "should-not-appear",
                "display_name": "should-not-appear",
                "model_type": "llm",
                "endpoint_url": "http://gpt-oss-20b:8000",
            }
        ]
    )
    payload_agents = json.dumps(
        [
            {
                "name": "should-not-agent",
                "endpoint_url": "http://agent:9000",
                "description_for_router": "no",
            }
        ]
    )
    monkeypatch.setenv("AUTO_REGISTER_MODELS", payload_models)
    monkeypatch.setenv("AUTO_REGISTER_AGENTS", payload_agents)
    if hasattr(auto_seed.settings, "AUTO_REGISTER_MODELS"):
        monkeypatch.setattr(auto_seed.settings, "AUTO_REGISTER_MODELS", payload_models)
        monkeypatch.setattr(auto_seed.settings, "AUTO_REGISTER_AGENTS", payload_agents)

    auto_seed.auto_seed()

    names = {row.name for row in db.query(ModelRegistry).all()}
    assert names == {"anila-router"}
    assert db.query(Agent).count() == 0


def test_empty_registry_reports_every_role_unset(client, db):
    make_user(db, username="role-empty-admin", role="admin")
    headers = {"Authorization": f"Bearer {login(client, 'role-empty-admin')}"}
    resp = client.get("/api/models/roles", headers=headers)
    assert resp.status_code == 200, resp.text
    roles = resp.json()["roles"]
    assert roles, "角色清單不該是空的"
    for role in roles:
        assert role["status"] == "unset", role
        assert role["model"] is None
        assert role.get("message")
