# -*- coding: utf-8 -*-
"""機敏分類盤點端點測試(SYSTEM-MAP §8 四級字彙)。

覆蓋:
- admin 取得的計數與 seeded fixtures 相符(含一列刻意 backfill 不一致);
- 非 admin(一般 user)拿到 403;
- ``?format=csv`` 回 text/csv 且以 UTF-8 BOM 開頭。
"""

from __future__ import annotations

import os

# 走 lifespan startup 的 TestClient 會撞 assert_no_dev_defaults;測試環境用
# dev 預設 SECRET_KEY/ADMIN_PASSWORD,需明示放行(同 test_tasks_module.py)。
os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from app.models.conversation import Conversation
from app.models.message import Message
from app.models.agent import Agent

from tests.conftest import make_user, login


def _seed(db):
    """種下可預期的分類分佈,含一列刻意不一致。"""
    owner = make_user(db, username="alice", role="user")

    # 三則對話:機密(已閂鎖)、無機密、以及「classified=True 但等級仍無機密」
    # 的刻意不一致列(backfill 尚未套用的髒資料)。
    from datetime import datetime, timezone
    now = datetime(2026, 7, 2, tzinfo=timezone.utc)
    db.add(Conversation(
        user_id=owner.id, title="c-secret",
        classified=True, classification_level="機密",
        classification_latched_at=now,
    ))
    db.add(Conversation(
        user_id=owner.id, title="c-plain",
        classified=False, classification_level="無機密",
    ))
    inconsistent = Conversation(
        user_id=owner.id, title="c-inconsistent",
        # 舊 latch 真,新等級卻 < 密(RESTRICTED) → inconsistent
        classified=True, classification_level="無機密",
    )
    db.add(inconsistent)
    db.commit()
    db.refresh(inconsistent)

    # 一則「密」訊息(已閂鎖)掛在不一致對話下。
    db.add(Message(
        conversation_id=inconsistent.id, role="user", content="x",
        classification_level="密", classification_latched_at=now,
    ))

    # 一個 agent:requires_encryption=True 但 default_classification_level 仍
    # 無機密 → agents 也應計一列不一致。
    db.add(Agent(
        name="enc-agent", owner_user_id=owner.id,
        endpoint_url="http://agent:9100", description_for_router="x",
        requires_encryption=True, default_classification_level="無機密",
    ))
    db.commit()
    return owner


def _by_type(payload):
    return {r["resource_type"]: r for r in payload["resources"]}


def test_admin_inventory_counts_match_fixtures(client, db):
    _seed(db)
    make_user(db, username="boss", role="admin")
    token = login(client, "boss")
    resp = client.get(
        "/api/classification/inventory",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert "generated_at" in payload
    rows = _by_type(payload)

    conv = rows["conversations"]
    assert conv["total"] == 3
    assert conv["levels"]["機密"] == 1
    assert conv["levels"]["無機密"] == 2
    assert conv["latched"] == 1
    # classified=True 且等級 < 密 的那一列 → inconsistent 應為 1。
    assert conv["inconsistent"] == 1

    msg = rows["messages"]
    assert msg["total"] == 1
    assert msg["levels"]["密"] == 1
    assert msg["latched"] == 1
    assert msg["inconsistent"] == 0  # messages 無舊 boolean → 恆 0

    agents = rows["agents"]
    assert agents["inconsistent"] == 1  # requires_encryption=True 但等級無機密

    # model_registry 現況無分類欄位 → 全數落在 floor 無機密,latched/inconsistent 0。
    reg = rows["model_registry"]
    assert reg["latched"] == 0
    assert reg["inconsistent"] == 0

    # 八個核心資源都出現。
    assert set(rows) == {
        "conversations", "messages", "ingestion_collections",
        "ingestion_documents", "agents", "model_registry",
        "tasks", "source_snapshots",
    }


def test_non_admin_forbidden(client, db):
    _seed(db)  # alice = 一般 user
    token = login(client, "alice")
    resp = client.get(
        "/api/classification/inventory",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403, resp.text


def test_csv_export_content_type_and_bom(client, db):
    _seed(db)
    make_user(db, username="boss", role="admin")
    token = login(client, "boss")
    resp = client.get(
        "/api/classification/inventory",
        params={"format": "csv"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    # UTF-8 BOM 供 Excel 正確辨識。
    assert resp.content.startswith(b"\xef\xbb\xbf")
    text = resp.content.decode("utf-8-sig")
    assert "資源類型" in text
    assert "conversations" in text
