"""P4.9 — agent must not bind a collection above its own classification level.

Owner ruling: agent level ≥ collection level; multi-bind checks the max;
refuse with a message naming the collection and both levels (never silently
raise the agent). Validate on the DELTA only — grandfathered over-level
bindings must not brick unrelated edits. Self-service: the registrant may
raise their own agent (G9), then retry the bind.
"""

from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest

from app.models.ingestion import IngestionCollection
from tests.conftest import login, make_agent, make_model, make_user


@pytest.fixture(autouse=True)
def _env_allowances(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "agent,mock-llm")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _collection(db, owner, name: str, *, level: str = "無機密") -> IngestionCollection:
    c = IngestionCollection(
        name=name,
        chunking_config={"strategy": "fixed"},
        embedding_model="nvidia/NV-embed-V2",
        embedding_dim=4000,
        created_by=owner.id,
        classification_level=level,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


class TestBindAboveAgentLevelRefused:
    def test_bind_above_level_refused_names_collection_and_levels(self, client, db):
        dev = make_user(db, username="p49_above", role="developer")
        agent = make_agent(db, dev, name="p49-above-agent")
        # Default agent level = 無機密
        secret_col = _collection(db, dev, "p49-secret-kb", level="機密")
        token = login(client, "p49_above")

        resp = client.put(
            f"/api/agents/{agent.id}",
            headers=_auth(token),
            json={"collection_ids": [secret_col.id]},
        )
        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"]
        assert "p49-secret-kb" in detail
        assert "機密" in detail
        assert "無機密" in detail
        assert "提升" in detail  # self-service remedy named

    def test_bind_at_or_below_accepted(self, client, db):
        dev = make_user(db, username="p49_ok", role="developer")
        agent = make_agent(db, dev, name="p49-ok-agent")
        agent.default_classification_level = "密"
        agent.requires_encryption = True
        db.commit()
        low = _collection(db, dev, "p49-low", level="營業秘密")
        same = _collection(db, dev, "p49-same", level="密")
        token = login(client, "p49_ok")

        resp = client.put(
            f"/api/agents/{agent.id}",
            headers=_auth(token),
            json={"collection_ids": [low.id, same.id]},
        )
        assert resp.status_code == 200, resp.text
        assert sorted(resp.json()["bound_collection_ids"]) == sorted(
            [low.id, same.id]
        )

    def test_multi_collection_checks_maximum(self, client, db):
        """One over-level among several → refuse citing the highest offender."""
        dev = make_user(db, username="p49_max", role="developer")
        agent = make_agent(db, dev, name="p49-max-agent")
        agent.default_classification_level = "營業秘密"
        agent.requires_encryption = False
        db.commit()
        ok = _collection(db, dev, "p49-max-ok", level="無機密")
        mid = _collection(db, dev, "p49-max-mid", level="密")
        high = _collection(db, dev, "p49-max-high", level="機密")
        token = login(client, "p49_max")

        resp = client.put(
            f"/api/agents/{agent.id}",
            headers=_auth(token),
            json={"collection_ids": [ok.id, mid.id, high.id]},
        )
        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"]
        assert "p49-max-high" in detail
        assert "機密" in detail
        assert "營業秘密" in detail


class TestSelfServiceUnblock:
    def test_developer_raises_own_agent_then_binds(self, client, db):
        """Blocked bind → owner raises agent (no admin) → bind succeeds."""
        dev = make_user(db, username="p49_self", role="developer")
        agent = make_agent(db, dev, name="p49-self-agent")
        col = _collection(db, dev, "p49-self-kb", level="營業秘密")
        token = login(client, "p49_self")

        blocked = client.put(
            f"/api/agents/{agent.id}",
            headers=_auth(token),
            json={"collection_ids": [col.id]},
        )
        assert blocked.status_code == 403, blocked.text

        raised = client.post(
            f"/api/agents/{agent.id}/classification",
            headers=_auth(token),
            json={"default_classification_level": "營業秘密"},
        )
        assert raised.status_code == 200, raised.text

        ok = client.put(
            f"/api/agents/{agent.id}",
            headers=_auth(token),
            json={"collection_ids": [col.id]},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["bound_collection_ids"] == [col.id]


class TestGrandfatheredOverLevel:
    def test_existing_over_level_binding_does_not_block_unrelated_edit(
        self, client, db
    ):
        """Pre-rule over-level binding: editing description must still work."""
        from app.services.agent_collection_bindings import set_bound_collection_ids

        dev = make_user(db, username="p49_gf", role="developer")
        agent = make_agent(db, dev, name="p49-gf-agent")
        # Agent stays 無機密; bind a 機密 collection directly (pre-rule row).
        high = _collection(db, dev, "p49-gf-kb", level="機密")
        set_bound_collection_ids(db, agent, [high.id])
        db.commit()
        db.refresh(agent)

        token = login(client, "p49_gf")
        resp = client.put(
            f"/api/agents/{agent.id}",
            headers=_auth(token),
            json={"description_for_router": "unrelated edit after grandfather"},
        )
        assert resp.status_code == 200, resp.text
        assert (
            resp.json()["description_for_router"]
            == "unrelated edit after grandfather"
        )
        assert resp.json()["bound_collection_ids"] == [high.id]

    def test_resend_same_over_level_set_without_delta_succeeds(self, client, db):
        """Console resend of the full set (no new ids) must not re-gate."""
        from app.services.agent_collection_bindings import set_bound_collection_ids

        dev = make_user(db, username="p49_resend", role="developer")
        agent = make_agent(db, dev, name="p49-resend-agent")
        high = _collection(db, dev, "p49-resend-kb", level="機密")
        set_bound_collection_ids(db, agent, [high.id])
        db.commit()

        token = login(client, "p49_resend")
        resp = client.put(
            f"/api/agents/{agent.id}",
            headers=_auth(token),
            json={"collection_ids": [high.id]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["bound_collection_ids"] == [high.id]


class TestRegisterGate:
    def test_register_with_over_level_collection_refused(self, client, db):
        dev = make_user(db, username="p49_reg", role="developer")
        model = make_model(db, name="p49-reg-model")
        col = _collection(db, dev, "p49-reg-kb", level="密")
        token = login(client, "p49_reg")

        resp = client.post(
            "/api/agents/register",
            headers=_auth(token),
            json={
                "name": "p49-reg-agent",
                "endpoint_url": "http://agent:9100",
                "description_for_router": "P4.9 register gate",
                "base_model_id": model.id,
                "default_classification_level": "無機密",
                "collection_ids": [col.id],
            },
        )
        assert resp.status_code == 403, resp.text
        assert "p49-reg-kb" in resp.json()["detail"]
        assert "密" in resp.json()["detail"]
