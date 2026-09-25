"""CSP persistence-boundary zh-TW 靜默正規化 hook。

覆蓋 append / branch / update_message_content / memory chunk 寫入，
以及 flag-off 與 user-role 原文不動。
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.message import Message
from app.services import memory_service
from tests.conftest import login, make_user

SIMPLIFIED = "机关单位的视频质量"
EXPECTED_TW = "機關單位的影片質量"


@pytest.fixture(autouse=True)
def _bypass_dev_secret_gate(monkeypatch):
    import app.services.startup_security as ss_module

    monkeypatch.setattr(ss_module, "assert_no_dev_defaults", lambda: None)


def _auth(client: TestClient, db: Session, username: str):
    make_user(db, username=username, role="user")
    return {"Authorization": f"Bearer {login(client, username=username)}"}


def _create_conv(client: TestClient, headers: dict) -> int:
    resp = client.post(
        "/api/conversations", json={"title": "zh-norm"}, headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_assistant_append_stores_traditional(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("ANILA_ZH_NORMALIZE", raising=False)
    h = _auth(client, db, "zh_asst")
    cid = _create_conv(client, h)
    resp = client.post(
        f"/api/conversations/{cid}/messages",
        json={"role": "assistant", "content": SIMPLIFIED},
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["content"] == EXPECTED_TW
    row = db.query(Message).filter(Message.id == resp.json()["id"]).one()
    assert row.content == EXPECTED_TW


def test_user_append_stores_verbatim(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("ANILA_ZH_NORMALIZE", raising=False)
    h = _auth(client, db, "zh_user")
    cid = _create_conv(client, h)
    resp = client.post(
        f"/api/conversations/{cid}/messages",
        json={"role": "user", "content": SIMPLIFIED},
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["content"] == SIMPLIFIED
    row = db.query(Message).filter(Message.id == resp.json()["id"]).one()
    assert row.content == SIMPLIFIED


def test_flag_zero_disables(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("ANILA_ZH_NORMALIZE", "0")
    h = _auth(client, db, "zh_off")
    cid = _create_conv(client, h)
    resp = client.post(
        f"/api/conversations/{cid}/messages",
        json={"role": "assistant", "content": SIMPLIFIED},
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["content"] == SIMPLIFIED


def test_assistant_branch_stores_normalized(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("ANILA_ZH_NORMALIZE", raising=False)
    h = _auth(client, db, "zh_branch")
    cid = _create_conv(client, h)
    seed = client.post(
        f"/api/conversations/{cid}/messages",
        json={"role": "assistant", "content": "seed"},
        headers=h,
    )
    assert seed.status_code == 201, seed.text
    mid = seed.json()["id"]
    resp = client.post(
        f"/api/conversations/{cid}/messages/{mid}/branch",
        json={"role": "assistant", "content": SIMPLIFIED},
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["content"] == EXPECTED_TW
    row = db.query(Message).filter(Message.id == resp.json()["id"]).one()
    assert row.content == EXPECTED_TW


def test_user_branch_stores_verbatim(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("ANILA_ZH_NORMALIZE", raising=False)
    h = _auth(client, db, "zh_ubranch")
    cid = _create_conv(client, h)
    seed = client.post(
        f"/api/conversations/{cid}/messages",
        json={"role": "user", "content": "seed"},
        headers=h,
    )
    assert seed.status_code == 201, seed.text
    mid = seed.json()["id"]
    resp = client.post(
        f"/api/conversations/{cid}/messages/{mid}/branch",
        json={"role": "user", "content": SIMPLIFIED},
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["content"] == SIMPLIFIED


def test_update_message_content_normalizes_assistant(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("ANILA_ZH_NORMALIZE", raising=False)
    h = _auth(client, db, "zh_upd")
    cid = _create_conv(client, h)
    seed = client.post(
        f"/api/conversations/{cid}/messages",
        json={"role": "assistant", "content": "seed"},
        headers=h,
    )
    assert seed.status_code == 201, seed.text
    mid = seed.json()["id"]
    resp = client.put(
        f"/api/conversations/{cid}/messages/{mid}",
        json={"content": SIMPLIFIED},
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == EXPECTED_TW
    row = db.query(Message).filter(Message.id == mid).one()
    assert row.content == EXPECTED_TW


def test_update_message_content_user_verbatim(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("ANILA_ZH_NORMALIZE", raising=False)
    h = _auth(client, db, "zh_uupd")
    cid = _create_conv(client, h)
    seed = client.post(
        f"/api/conversations/{cid}/messages",
        json={"role": "user", "content": "seed"},
        headers=h,
    )
    assert seed.status_code == 201, seed.text
    mid = seed.json()["id"]
    resp = client.put(
        f"/api/conversations/{cid}/messages/{mid}",
        json={"content": SIMPLIFIED},
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == SIMPLIFIED


def test_persist_turn_does_not_store_assistant_text(
    monkeypatch: pytest.MonkeyPatch,
):
    """記憶不再把助理回答存成片段，正規化後的原文也不落庫。"""
    monkeypatch.delenv("ANILA_ZH_NORMALIZE", raising=False)
    captured: dict = {}

    async def fake_embed(
        db,
        text_input,
        *,
        user_id: int = 0,
        department_id: int | None = None,
        embedding_input_role: str = "query",
    ):
        # 這個 stub 必須與 memory_service._embed 的真實簽章一致；只寫
        # (db, text_input) 會在生產端新增具名參數時變成 TypeError。
        captured.setdefault("roles", []).append(embedding_input_role)
        return [0.1] * 8, "nvidia/nv-embed-v2", 8

    def track_insert(db, **kwargs):
        captured.setdefault("chunks", []).append(
            {"role": kwargs["role"], "content": kwargs["content"]}
        )

    monkeypatch.setattr(memory_service, "SessionLocal", lambda: _DummySession())
    monkeypatch.setattr(memory_service, "_embed", fake_embed)
    monkeypatch.setattr(memory_service, "_insert_chunk", track_insert)
    monkeypatch.setattr(
        memory_service, "_extract_facts", AsyncMock(return_value=[])
    )

    asyncio.run(
        memory_service.persist_turn(
            user_id=1,
            conversation_id=1,
            user_message=SIMPLIFIED,
            assistant_message=SIMPLIFIED,
            is_encrypted=False,
            user_message_id=10,
            assistant_message_id=11,
        )
    )

    assert captured.get("chunks", []) == []
    assert captured.get("roles", []) == []


def test_write_chunk_adapter_normalizes_assistant(
    monkeypatch: pytest.MonkeyPatch,
):
    """PostgresMemoryAdapter.write_chunk → _write_chunk normalizes assistant."""
    monkeypatch.delenv("ANILA_ZH_NORMALIZE", raising=False)
    captured: dict = {}

    async def fake_embed(
        db,
        text_input,
        *,
        user_id: int = 0,
        department_id: int | None = None,
        embedding_input_role: str = "query",
    ):
        captured["embedded"] = text_input
        captured["role"] = embedding_input_role
        return [0.1] * 8, "nvidia/nv-embed-v2", 8

    def track_insert(db, **kwargs):
        captured.setdefault("chunks", []).append(
            {"role": kwargs["role"], "content": kwargs["content"]}
        )

    monkeypatch.setattr(memory_service, "_embed", fake_embed)
    monkeypatch.setattr(memory_service, "_insert_chunk", track_insert)

    adapter = memory_service.PostgresMemoryAdapter(
        db_factory=lambda: _DummySession()
    )
    asyncio.run(
        adapter.write_chunk(
            user_id=1,
            conversation_id=1,
            message_id=42,
            role="assistant",
            content=SIMPLIFIED,
            is_encrypted=False,
        )
    )

    assert len(captured["chunks"]) == 1
    assert captured["chunks"][0]["role"] == "assistant"
    assert captured["chunks"][0]["content"] == EXPECTED_TW
    assert captured["embedded"] == EXPECTED_TW
    assert captured["role"] == "document"


class _DummySession:
    """Minimal stand-in: persist_turn only needs commit/rollback/close/get.

    ``get`` 是 Task 2 之後才需要的：正規化開關 ``intl.zh_normalize`` 現在每次
    呼叫都查一次 ``platform_settings``。回 ``None`` ＝「這個 key 沒有那一列」，
    於是解析退到 env、再退到程式預設（開）—— 正是本檔要測的那個姿態。
    """

    def get(self, *_args, **_kwargs):
        return None

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None

    def execute(self, *args, **kwargs):
        return None
