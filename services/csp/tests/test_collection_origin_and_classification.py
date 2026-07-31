"""知識庫建立密等 + origin 產品面邊界。

Problem 1: 建立時可選四級密等，寫入後就是 agent 綁定規則比對的那一欄。
Problem 2: 比照 conversations.origin —— CSP / ANILALM 各自貨架；
pre-origin 舊列（origin IS NULL）對擁有者在兩邊都還看得到。
"""

from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest

from app.models.ingestion import IngestionCollection
from tests.conftest import login, make_agent, make_user


@pytest.fixture(autouse=True)
def _env_allowances(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "agent,mock-llm")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_payload(
    name: str,
    *,
    origin: str | None = None,
    classification_level: str = "無機密",
) -> dict:
    body: dict = {
        "name": name,
        "chunking_config": {"strategy": "fixed", "params": {"size": 256}},
        "classification_level": classification_level,
    }
    if origin is not None:
        body["origin"] = origin
    return body


class TestCreateStoresChosenClassification:
    def test_create_with_level_stores_and_bind_rule_compares_it(self, client, db):
        """AC1: 建立時選定的密等會入庫，且就是綁定規則比對的那一值。"""
        dev = make_user(db, username="coll_cls_create", role="developer")
        agent = make_agent(db, dev, name="coll-cls-agent")
        # agent 預設無機密；綁「密」知識庫必須被拒。
        token = login(client, "coll_cls_create")

        created = client.post(
            "/api/ingestion/collections",
            headers=_auth(token),
            json=_create_payload(
                "coll-cls-kb",
                origin="csp",
                classification_level="密",
            ),
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["classification_level"] == "密"
        assert body["origin"] == "csp"

        row = db.get(IngestionCollection, body["id"])
        assert row is not None
        assert row.classification_level == "密"

        refused = client.put(
            f"/api/agents/{agent.id}",
            headers=_auth(token),
            json={"collection_ids": [body["id"]]},
        )
        assert refused.status_code == 403, refused.text
        detail = refused.json()["detail"]
        assert "coll-cls-kb" in detail
        assert "密" in detail
        assert "無機密" in detail
        assert "提升" in detail


class TestBindRefusesBelowCollectionLevel:
    def test_bind_refuses_with_actionable_reason(self, client, db):
        """AC2: agent 等級低於知識庫 → 403，理由可據以自救。"""
        # Production edit that turns this red: delete the
        # ``if agent_level < col_level`` raise in refuse_bind_above_agent_level.
        dev = make_user(db, username="coll_bind_reason", role="developer")
        agent = make_agent(db, dev, name="coll-bind-agent")
        token = login(client, "coll_bind_reason")

        created = client.post(
            "/api/ingestion/collections",
            headers=_auth(token),
            json=_create_payload(
                "coll-bind-secret",
                origin="csp",
                classification_level="機密",
            ),
        )
        assert created.status_code == 201, created.text
        cid = created.json()["id"]

        resp = client.put(
            f"/api/agents/{agent.id}",
            headers=_auth(token),
            json={"collection_ids": [cid]},
        )
        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"]
        assert "coll-bind-secret" in detail
        assert "機密" in detail
        assert "無機密" in detail
        assert "提升" in detail


class TestOriginSurfacePartition:
    def test_created_in_one_app_not_listed_by_the_other(self, client, db):
        """AC3a: CSP 建的不出現在 anilalm 列表，反之亦然。"""
        user = make_user(db, username="coll_origin_part", role="developer")
        token = login(client, "coll_origin_part")
        h = _auth(token)

        csp = client.post(
            "/api/ingestion/collections",
            headers=h,
            json=_create_payload("csp-only-kb", origin="csp"),
        )
        assert csp.status_code == 201, csp.text
        lm = client.post(
            "/api/ingestion/collections",
            headers=h,
            json=_create_payload("anilalm-only-kb", origin="anilalm"),
        )
        assert lm.status_code == 201, lm.text

        csp_list = client.get(
            "/api/ingestion/collections",
            headers=h,
            params={"origin": "csp"},
        )
        assert csp_list.status_code == 200, csp_list.text
        csp_names = {row["name"] for row in csp_list.json()}
        assert "csp-only-kb" in csp_names
        assert "anilalm-only-kb" not in csp_names

        lm_list = client.get(
            "/api/ingestion/collections",
            headers=h,
            params={"origin": "anilalm"},
        )
        assert lm_list.status_code == 200, lm_list.text
        lm_names = {row["name"] for row in lm_list.json()}
        assert "anilalm-only-kb" in lm_names
        assert "csp-only-kb" not in lm_names

    def test_legacy_null_origin_still_reachable_by_owner(self, client, db):
        """AC3b: 既有 origin=NULL 列，擁有者在兩邊列表都還看得到。"""
        user = make_user(db, username="coll_origin_legacy", role="developer")
        legacy = IngestionCollection(
            name="legacy-pre-origin-kb",
            chunking_config={"strategy": "fixed", "params": {}},
            embedding_model="nvidia/NV-embed-V2",
            embedding_dim=4000,
            created_by=user.id,
            origin=None,
        )
        db.add(legacy)
        db.commit()
        db.refresh(legacy)

        token = login(client, "coll_origin_legacy")
        h = _auth(token)

        for surface in ("csp", "anilalm"):
            resp = client.get(
                "/api/ingestion/collections",
                headers=h,
                params={"origin": surface},
            )
            assert resp.status_code == 200, resp.text
            ids = {row["id"] for row in resp.json()}
            assert legacy.id in ids, f"legacy missing under origin={surface}"
