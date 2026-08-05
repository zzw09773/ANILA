"""知識庫升密路由：單向閂鎖、文件級聯、綁定 agent 擋下。

AC:
1. 無機密 → 機密：欄位變更、ClassificationEvent、audit；降級被拒且訊息指向降密流程
2. 已綁定較低密等 agent → 拒升並點名 agent；不靜默提升 agent
3. 升密後文件有效密等 ≥ 知識庫（級聯經 apply_classification）
"""

from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest

from app.models.audit_log import AuditLog
from app.models.classification import ClassificationEvent
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.services.ingestion_classification import (
    effective_document_classification_level,
)
from tests.conftest import login, make_agent, make_user


@pytest.fixture(autouse=True)
def _env_allowances(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "agent,mock-llm")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_payload(name: str, *, level: str = "無機密") -> dict:
    return {
        "name": name,
        "chunking_config": {"strategy": "fixed", "params": {"size": 256}},
        "classification_level": level,
        "origin": "csp",
    }


def _add_document(db, collection: IngestionCollection, filename: str) -> IngestionDocument:
    doc = IngestionDocument(
        collection_id=collection.id,
        filename=filename,
        sha256=("a" * 64),
        status="pending",
        classification_level="無機密",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


class TestRaiseCollectionClassification:
    def test_raise_writes_level_event_and_audit_refuses_lower(self, client, db):
        user = make_user(db, username="coll_raise_ok", role="developer")
        token = login(client, "coll_raise_ok")
        h = _auth(token)

        created = client.post(
            "/api/ingestion/collections",
            headers=h,
            json=_create_payload("raise-kb-ok", level="無機密"),
        )
        assert created.status_code == 201, created.text
        cid = created.json()["id"]

        raised = client.post(
            f"/api/ingestion/collections/{cid}/classification",
            headers=h,
            json={"classification_level": "機密"},
        )
        assert raised.status_code == 200, raised.text
        body = raised.json()
        assert body["classification_level"] == "機密"

        row = db.get(IngestionCollection, cid)
        assert row is not None
        assert row.classification_level == "機密"

        events = (
            db.query(ClassificationEvent)
            .filter(
                ClassificationEvent.resource_type == "collection",
                ClassificationEvent.resource_id == str(cid),
            )
            .all()
        )
        assert len(events) >= 1
        assert any(e.new_level == "機密" for e in events)

        audits = (
            db.query(AuditLog)
            .filter(
                AuditLog.action == "ingestion_collection_classification_raise",
                AuditLog.resource_id == str(cid),
            )
            .all()
        )
        # resource_id type may be int or str depending on AuditLog column
        if not audits:
            audits = (
                db.query(AuditLog)
                .filter(AuditLog.action == "ingestion_collection_classification_raise")
                .all()
            )
            audits = [
                a
                for a in audits
                if str(getattr(a, "resource_id", "")) == str(cid)
            ]
        assert audits, "expected audit row for collection raise"

        lower = client.post(
            f"/api/ingestion/collections/{cid}/classification",
            headers=h,
            json={"classification_level": "無機密"},
        )
        assert lower.status_code == 409, lower.text
        detail = lower.json()["detail"]
        assert "降密" in detail or "降級" in detail
        assert "declassification" in detail or "降密申請" in detail

        # Same-level also refused (not a silent 200).
        same = client.post(
            f"/api/ingestion/collections/{cid}/classification",
            headers=h,
            json={"classification_level": "機密"},
        )
        assert same.status_code == 409, same.text

    def test_stranded_bind_refuses_and_names_agent(self, client, db):
        """Collection at 無機密 + bound agent at 無機密 → raise to 機密 refused."""
        dev = make_user(db, username="coll_raise_bind", role="developer")
        agent = make_agent(db, dev, name="stranded-low-agent")
        # Default agent level = 無機密
        token = login(client, "coll_raise_bind")
        h = _auth(token)

        created = client.post(
            "/api/ingestion/collections",
            headers=h,
            json=_create_payload("stranded-kb", level="無機密"),
        )
        assert created.status_code == 201, created.text
        cid = created.json()["id"]

        bound = client.put(
            f"/api/agents/{agent.id}",
            headers=h,
            json={"collection_ids": [cid]},
        )
        assert bound.status_code == 200, bound.text

        refused = client.post(
            f"/api/ingestion/collections/{cid}/classification",
            headers=h,
            json={"classification_level": "機密"},
        )
        assert refused.status_code == 409, refused.text
        detail = refused.json()["detail"]
        assert "stranded-low-agent" in detail
        assert "機密" in detail
        assert "不會自動提升" in detail or "classification" in detail

        # Agent must NOT have been silently raised.
        db.refresh(agent)
        assert (agent.default_classification_level or "無機密") == "無機密"

        # Collection stays at 無機密.
        row = db.get(IngestionCollection, cid)
        assert row.classification_level == "無機密"

    def test_document_cascades_on_raise(self, client, db):
        user = make_user(db, username="coll_raise_doc", role="developer")
        token = login(client, "coll_raise_doc")
        h = _auth(token)

        created = client.post(
            "/api/ingestion/collections",
            headers=h,
            json=_create_payload("cascade-kb", level="無機密"),
        )
        assert created.status_code == 201, created.text
        cid = created.json()["id"]
        coll = db.get(IngestionCollection, cid)
        doc = _add_document(db, coll, "notes.txt")
        assert doc.classification_level == "無機密"

        raised = client.post(
            f"/api/ingestion/collections/{cid}/classification",
            headers=h,
            json={"classification_level": "密"},
        )
        assert raised.status_code == 200, raised.text

        db.refresh(doc)
        db.refresh(coll)
        assert coll.classification_level == "密"
        assert doc.classification_level == "密"
        assert (
            effective_document_classification_level(doc, coll).to_storage()
            == "密"
        )

        doc_events = (
            db.query(ClassificationEvent)
            .filter(
                ClassificationEvent.resource_type == "document",
                ClassificationEvent.resource_id == str(doc.id),
            )
            .all()
        )
        assert any(e.new_level == "密" for e in doc_events)

        listed = client.get(
            f"/api/ingestion/collections/{cid}/documents",
            headers=h,
        )
        assert listed.status_code == 200, listed.text
        assert listed.json()[0]["classification_level"] == "密"

    def test_patch_rejects_classification_level_loudly(self, client, db):
        """PATCH 帶密等 → **422**，不是「200 但欄位被靜默忽略」。

        Pydantic 預設會丟掉未知鍵，所以原本會回 200、名字改了、密等沒改、
        什麼也沒說——正是本專案列為最糟的失敗模式（使用者以為改了）。
        這支測試把裁決釘住：只接受 422，訊息要指向正確的路由，而且整個
        PATCH 一起被拒（name 也不得被寫進去）。
        """
        user = make_user(db, username="coll_raise_patch", role="developer")
        token = login(client, "coll_raise_patch")
        h = _auth(token)
        created = client.post(
            "/api/ingestion/collections",
            headers=h,
            json=_create_payload("patch-no-cls", level="無機密"),
        )
        cid = created.json()["id"]
        patched = client.patch(
            f"/api/ingestion/collections/{cid}",
            headers=h,
            json={"classification_level": "機密", "name": "patch-no-cls-renamed"},
        )
        assert patched.status_code == 422, patched.text
        assert "classification" in patched.text

        row = db.get(IngestionCollection, cid)
        db.refresh(row)
        assert row.classification_level == "無機密"
        assert row.name == "patch-no-cls", "422 之後不該有任何欄位被寫入"

        # 不帶密等的 PATCH 仍然正常。
        ok = client.patch(
            f"/api/ingestion/collections/{cid}",
            headers=h,
            json={"name": "patch-no-cls-renamed"},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["name"] == "patch-no-cls-renamed"
        assert ok.json()["classification_level"] == "無機密"
