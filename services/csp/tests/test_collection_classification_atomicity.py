"""知識庫升密是全有全無——失敗不得留下已閂鎖的文件。

閂鎖是單向的，所以「一半的升密」不是可以重試掉的小事：被升上去的文件
只能靠三方降密流程一筆一筆撈回來，而呼叫端看到的是失敗、直覺是重試，
重試會成功並看起來一切正常，那幾筆文件就永遠停在高一級。

因此升密路由把「庫內文件級聯 + 知識庫本身閂鎖 + audit」放進**同一個交易**，
只有一個 ``db.commit()``。本檔驗證三種失敗都回到原點：

1. 文件密等儲存值壞掉（前置檢查整批拒絕）
2. 級聯途中丟例外（交易 rollback）
3. 知識庫閂鎖回 ``None``（並行升密／已在該等級）——回 409 且文件不得已被升

⚠ 原子性**不依賴 row locking**。``apply_classification`` 確實會下
``SELECT … FOR UPDATE``（SQLite 是 no-op），但讓這件事全有全無的是那個
單一外層交易；列鎖只縮小並行升密的窗口，而並行情境由案例 3 明確處理。
所以這些測試在沒有 Postgres 的環境下驗的仍然是真的不變式。
"""

from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest

from app.api.ingestion import collections as collections_api
from app.models.classification import ClassificationEvent
from app.models.ingestion import IngestionCollection, IngestionDocument
from tests.conftest import login, make_user


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _collection(db, owner, *, name: str, level: str = "無機密"):
    coll = IngestionCollection(
        name=name,
        embedding_model="test-embed",
        embedding_dim=8,
        chunking_config={"strategy": "fixed", "params": {"size": 256}},
        classification_level=level,
        created_by=owner.id,
        origin="csp",
    )
    db.add(coll)
    db.commit()
    db.refresh(coll)
    return coll


def _documents(db, coll, levels: list[str]) -> list[IngestionDocument]:
    docs = []
    for i, level in enumerate(levels):
        doc = IngestionDocument(
            collection_id=coll.id,
            filename=f"doc{i + 1}.txt",
            sha256=f"{i:064d}",
            status="pending",
            classification_level=level,
        )
        db.add(doc)
        docs.append(doc)
    db.commit()
    for doc in docs:
        db.refresh(doc)
    return docs


def _assert_nothing_advanced(db, coll, docs, expected_levels: list[str]):
    """知識庫與每一份文件都停在原地，且沒有任何升密事件落地。"""
    db.expire_all()
    db.refresh(coll)
    assert coll.classification_level == "無機密", (
        f"知識庫被部分升密到 {coll.classification_level!r}"
    )
    for doc, expected in zip(docs, expected_levels):
        db.refresh(doc)
        assert doc.classification_level == expected, (
            f"{doc.filename} 被留在 {doc.classification_level!r}"
            f"（應為 {expected!r}）——半套升密，只能靠降密流程撈回來"
        )
    doc_ids = {str(d.id) for d in docs}
    stray = [
        e
        for e in db.query(ClassificationEvent)
        .filter(ClassificationEvent.resource_type == "document")
        .all()
        if e.resource_id in doc_ids
    ]
    assert stray == [], f"殘留 {len(stray)} 筆文件升密事件：{[e.resource_id for e in stray]}"


@pytest.fixture
def raiser(client, db):
    """一位使用者 + 一個無機密知識庫 + 四份無機密文件。"""
    user = make_user(db, username="atomic_raiser", role="developer")
    token = login(client, "atomic_raiser")
    coll = _collection(db, user, name="atomic-kb")
    docs = _documents(db, coll, ["無機密"] * 4)
    return {"user": user, "headers": _auth(token), "coll": coll, "docs": docs}


class TestRaiseIsAllOrNothing:
    def test_unreadable_document_value_refuses_the_whole_raise(
        self, client, db, raiser
    ):
        """壞掉的儲存值：整批拒絕、點名該列，不靜默跳過也不部分升密。

        靜默跳過會讓那份文件留在知識庫之下——不變式 (a) 反過來。
        """
        coll, docs, h = raiser["coll"], raiser["docs"], raiser["headers"]
        docs[2].classification_level = "GARBAGE"
        db.commit()

        resp = client.post(
            f"/api/ingestion/collections/{coll.id}/classification",
            headers=h,
            json={"classification_level": "機密"},
        )
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert f"#{docs[2].id}" in detail, detail
        assert "GARBAGE" in detail, detail
        assert "未做任何變更" in detail, detail

        _assert_nothing_advanced(
            db, coll, docs, ["無機密", "無機密", "GARBAGE", "無機密"]
        )

    def test_unreadable_collection_value_refuses_with_a_message(
        self, client, db, raiser
    ):
        """知識庫自己的儲存值壞掉：409 有訊息，不是沒有訊息的 500。"""
        coll, docs, h = raiser["coll"], raiser["docs"], raiser["headers"]
        coll.classification_level = "GARBAGE"
        db.commit()

        resp = client.post(
            f"/api/ingestion/collections/{coll.id}/classification",
            headers=h,
            json={"classification_level": "機密"},
        )
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert "GARBAGE" in detail, detail
        assert "未做任何變更" in detail, detail

        db.refresh(coll)
        assert coll.classification_level == "GARBAGE"
        for doc in docs:
            db.refresh(doc)
            assert doc.classification_level == "無機密"

    def test_failure_midway_through_the_cascade_rolls_everything_back(
        self, client, db, monkeypatch, raiser
    ):
        """級聯第三筆炸掉：前兩筆不得留在已升密狀態。

        這正是原始缺陷的形狀——doc1/doc2 各自 commit 過，呼叫端收到沒有
        訊息的 500，重試會成功並看起來正常，而那兩筆已經回不去了。
        """
        coll, docs, h = raiser["coll"], raiser["docs"], raiser["headers"]

        import app.modules.policy as policy_module

        real = policy_module.apply_classification
        calls = {"n": 0}

        def _boom(db_, **kwargs):
            if kwargs.get("resource_type") == "document":
                calls["n"] += 1
                if calls["n"] == 3:
                    raise RuntimeError("simulated cascade failure on document 3")
            return real(db_, **kwargs)

        monkeypatch.setattr(policy_module, "apply_classification", _boom)

        resp = client.post(
            f"/api/ingestion/collections/{coll.id}/classification",
            headers=h,
            json={"classification_level": "機密"},
        )
        assert resp.status_code == 500, resp.text
        detail = resp.json()["detail"]
        # 不是沒有訊息的 500：必須說清楚什麼都沒動。
        assert "已整批回復" in detail, detail
        assert "未變更" in detail, detail
        assert calls["n"] == 3, "前提壞了：級聯沒跑到第三筆"

        _assert_nothing_advanced(db, coll, docs, ["無機密"] * 4)

    def test_collection_latch_noop_rolls_the_cascade_back(
        self, client, db, monkeypatch, raiser
    ):
        """並行升密（知識庫閂鎖回 None）：409，且文件不得已被升上去。"""
        coll, docs, h = raiser["coll"], raiser["docs"], raiser["headers"]

        # 只換掉路由裡那一個名字——文件級聯仍走真的 apply_classification。
        monkeypatch.setattr(
            collections_api, "apply_classification", lambda db_, **kw: None
        )

        resp = client.post(
            f"/api/ingestion/collections/{coll.id}/classification",
            headers=h,
            json={"classification_level": "機密"},
        )
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert "知識庫密等未變更" in detail, detail
        assert "本次未變更任何文件密等" in detail, detail

        _assert_nothing_advanced(db, coll, docs, ["無機密"] * 4)

    def test_successful_raise_still_commits_everything_together(
        self, client, db, raiser
    ):
        """對照組：沒有失敗時，庫與四份文件一起升上去（事件也都在）。"""
        coll, docs, h = raiser["coll"], raiser["docs"], raiser["headers"]

        resp = client.post(
            f"/api/ingestion/collections/{coll.id}/classification",
            headers=h,
            json={"classification_level": "機密"},
        )
        assert resp.status_code == 200, resp.text

        db.expire_all()
        db.refresh(coll)
        assert coll.classification_level == "機密"
        for doc in docs:
            db.refresh(doc)
            assert doc.classification_level == "機密"
            assert doc.classification_latched_at is not None
            assert doc.classification_source == "collection_raise"
            assert doc.classification_event_id is not None
