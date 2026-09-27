# -*- coding: utf-8 -*-
"""聊天模型只有 llm。vlm 不是登錄類型。

能看圖與否不另開一種。既有 vlm 列由遷移改成 llm；新的註冊若再送
vlm，API 要拒絕並請對方改登記為 llm。改成 llm 之後，同一列可以當
主路由、簡報、摘要、知識庫對話，以及視覺角色。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.models.model_registry import ModelRegistry
from app.models.router_model_grant import RouterModelGrant
from app.schemas.model_registry import ModelCreate, ModelUpdate
from app.services.router_model_policy import ROUTER_ELIGIBLE_TYPES
from tests.conftest import login, make_user

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations/versions/r1_0053_drop_vlm_model_type.py"
)

_REJECTED = "沒有獨立的 vlm 類型，請登記為 llm"


def _migration():
    assert _MIGRATION.is_file(), f"缺少遷移 {_MIGRATION.name}"
    spec = importlib.util.spec_from_file_location("r1_0053_drop_vlm", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _llm(db, name, *, model_type="llm") -> ModelRegistry:
    row = ModelRegistry(
        name=name,
        display_name=name,
        model_type=model_type,
        endpoint_url="http://172.16.120.35:28080/v1",
        is_active=True,
        router_enabled=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_migration_converts_vlm_rows_and_downgrade_is_noop(db):
    """生產變更：遷移沒把 model_type='vlm' 改成 llm，或降版把 llm 改回去。"""
    former = _llm(db, "qwen-vl", model_type="vlm")
    kept_llm = _llm(db, "gemma-chat", model_type="llm")
    kept_embed = _llm(db, "nv-embed", model_type="embedding")

    module = _migration()
    assert module.revision == "r1_0053"
    assert module.down_revision == "r1_0052"
    module.convert_vlm_rows(db.connection())
    module.convert_vlm_rows(db.connection())
    db.commit()
    db.expire_all()

    assert former.model_type == "llm"
    assert kept_llm.model_type == "llm"
    assert kept_embed.model_type == "embedding"

    module.downgrade()
    db.expire_all()
    assert former.model_type == "llm"
    doc = (module.__doc__ or "") + (module.downgrade.__doc__ or "")
    assert "無法分辨" in doc


def test_registering_vlm_is_rejected(client, db):
    """生產變更：POST/PUT 仍接受 model_type=vlm。"""
    with pytest.raises(ValidationError) as created:
        ModelCreate(
            name="qwen-vl",
            display_name="Qwen VL",
            model_type="vlm",
            endpoint_url="https://gateway.example.com/v1",
        )
    assert _REJECTED in str(created.value)

    llm = ModelCreate(
        name="qwen-vl",
        display_name="Qwen VL",
        model_type="llm",
        endpoint_url="https://gateway.example.com/v1",
    )
    assert llm.model_type == "llm"

    with pytest.raises(ValidationError) as updated:
        ModelUpdate(model_type="vlm")
    assert _REJECTED in str(updated.value)
    assert ModelUpdate(model_type="llm").model_type == "llm"
    assert ModelUpdate().model_type is None

    make_user(db, username="vlm-owner", role="owner")
    headers = {"Authorization": f"Bearer {login(client, 'vlm-owner')}"}
    resp = client.post(
        "/api/models",
        json={
            "name": "qwen-vl-api",
            "display_name": "Qwen VL",
            "model_type": "vlm",
            "endpoint_url": "https://gateway.example.com/v1",
        },
        headers=headers,
    )
    assert resp.status_code == 422, resp.text
    assert _REJECTED in resp.text
    assert (
        db.query(ModelRegistry).filter(ModelRegistry.name == "qwen-vl-api").first()
        is None
    )

    row = _llm(db, "already-llm")
    put = client.put(
        f"/api/models/{row.id}",
        json={"model_type": "vlm"},
        headers=headers,
    )
    assert put.status_code == 422, put.text
    assert _REJECTED in put.text
    db.refresh(row)
    assert row.model_type == "llm"


def test_llm_can_fill_chat_roles_and_vision(client, db):
    """生產變更：視覺角色仍把 vlm 當獨立類型，或 llm 不能擔任這些角色。"""
    from app.services.model_roles import ROLE_SPECS
    from app.services.thinking_probe import _discoverable

    assert ROUTER_ELIGIBLE_TYPES == frozenset({"llm"})
    assert ROLE_SPECS["vision"].accepted_types == frozenset({"llm"})
    assert "vlm" not in ROLE_SPECS["vision"].description
    assert "語言模型" in ROLE_SPECS["vision"].description
    assert _discoverable(
        SimpleNamespace(model_type="llm", protocol="openai_compatible")
    ) is True
    assert _discoverable(
        SimpleNamespace(model_type="vlm", protocol="openai_compatible")
    ) is False

    model = _llm(db, "former-vlm")
    db.add(RouterModelGrant(model_id=model.id, scope_type="all"))
    db.commit()
    make_user(db, username="chat-role-owner", role="admin")
    headers = {"Authorization": f"Bearer {login(client, 'chat-role-owner')}"}

    for role in (
        "router_primary",
        "slides",
        "summary",
        "knowledge_chat",
        "vision",
    ):
        resp = client.put(
            f"/api/models/roles/{role}",
            json={"model_id": model.id},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        assert body["model"]["name"] == "former-vlm"
        assert "vlm" not in body["accepted_types"]

    listed = client.get("/api/models/roles", headers=headers)
    assert listed.status_code == 200, listed.text
    by_role = {row["role"]: row for row in listed.json()["roles"]}
    assert by_role["vision"]["accepted_types"] == ["llm"]
    for role in (
        "router_primary",
        "slides",
        "summary",
        "knowledge_chat",
        "vision",
    ):
        assert by_role[role]["model"]["name"] == "former-vlm"
        assert by_role[role]["status"] == "ok"


def test_auto_seed_does_not_store_vlm_entries_from_env(db, monkeypatch):
    """開機不再把 AUTO_REGISTER_MODELS 裡的 vlm 列寫進登錄表。"""
    import json

    from app.services import auto_seed

    class _KeepOpen:
        def __init__(self, session):
            self._session = session

        def close(self):
            return None

        def __getattr__(self, name):
            return getattr(self._session, name)

    monkeypatch.setattr(auto_seed, "SessionLocal", lambda: _KeepOpen(db))
    monkeypatch.setattr(auto_seed.settings, "AUTO_SEED_API_KEYS", "")
    monkeypatch.setenv(
        "AUTO_REGISTER_MODELS",
        json.dumps(
            [
                {
                    "name": "seed-qwen-vl",
                    "display_name": "Qwen VL",
                    "model_type": "vlm",
                    "endpoint_url": "http://qwen:8000/v1",
                }
            ]
        ),
    )
    auto_seed.auto_seed()
    assert (
        db.query(ModelRegistry).filter(ModelRegistry.name == "seed-qwen-vl").count()
        == 0
    )
