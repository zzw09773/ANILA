"""Tests for agent registration, approval, rejection, and permission assignment."""

from __future__ import annotations

import pytest

from tests.conftest import login, make_agent, make_model, make_user


@pytest.fixture(autouse=True)
def _trusted_agent_host(monkeypatch):
    """The dev registration fixture uses the Compose service name ``agent``."""
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "agent")
    monkeypatch.setenv("ANILA_ALLOW_HTTP_AGENT_ENDPOINT", "1")


class TestAgentRegistration:
    def test_developer_can_register_agent(self, client, db):
        dev = make_user(db, username="dev1", role="developer")
        base_model = make_model(db, name="agent-base-1")
        token = login(client, "dev1")

        resp = client.post(
            "/api/agents/register",
            json={
                "name": "my-agent",
                "endpoint_url": "http://agent:9100",
                "description_for_router": "Handles HR queries",
                "base_model_id": base_model.id,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["name"] == "my-agent"
        assert data["approval_status"] == "pending_connection_test"
        assert data["owner_user_id"] == dev.id
        assert data["classification_ceiling"] == "無機密"

    def test_registration_persists_explicit_ceiling_and_rejects_null(
        self, client, db
    ):
        make_user(db, username="dev-ceiling", role="developer")
        base_model = make_model(db, name="agent-base-ceiling")
        token = login(client, "dev-ceiling")
        payload = {
            "name": "ceiling-agent",
            "endpoint_url": "http://agent:9100",
            "description_for_router": "classified work",
            "base_model_id": base_model.id,
        }

        accepted = client.post(
            "/api/agents/register",
            json={**payload, "classification_ceiling": "極機密"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["classification_ceiling"] == "極機密"

        rejected = client.post(
            "/api/agents/register",
            json={**payload, "name": "null-agent", "classification_ceiling": None},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert rejected.status_code == 422

    def test_plain_user_cannot_register(self, client, db):
        make_user(db, username="user1", role="user")
        token = login(client, "user1")

        resp = client.post(
            "/api/agents/register",
            json={
                "name": "sneaky-agent",
                "endpoint_url": "http://x:9100",
                "description_for_router": "...",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    def test_duplicate_name_rejected(self, client, db):
        dev = make_user(db, username="dev2", role="developer")
        base_model = make_model(db, name="agent-base-2")
        make_agent(db, dev, name="dup-agent")
        token = login(client, "dev2")

        resp = client.post(
            "/api/agents/register",
            json={
                "name": "dup-agent",
                "endpoint_url": "http://agent:9100",
                "description_for_router": "...",
                "base_model_id": base_model.id,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400

    def test_admin_can_approve(self, client, db):
        from datetime import datetime, timezone

        dev = make_user(db, username="dev3", role="developer")
        admin = make_user(db, username="admin3", role="admin")
        agent = make_agent(db, dev, name="pending-agent")
        # Slice 5a: approve is now blocked until Full Trace passed. Put the
        # agent at the security-review gate with a trace-test stamp so this
        # legacy happy-path stays green under the new 7-value state machine.
        agent.approval_status = "pending_security_review"
        agent.trace_test_passed_at = datetime.now(timezone.utc)
        db.commit()
        token = login(client, "admin3")

        resp = client.post(
            f"/api/agents/{agent.id}/approve",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        db.refresh(agent)
        assert agent.approval_status == "approved"
        assert agent.approved_by == admin.id

    def test_admin_can_reject(self, client, db):
        dev = make_user(db, username="dev4", role="developer")
        make_user(db, username="admin4", role="admin")
        agent = make_agent(db, dev, name="bad-agent")
        token = login(client, "admin4")

        resp = client.post(
            f"/api/agents/{agent.id}/reject",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        db.refresh(agent)
        assert agent.approval_status == "rejected"

    def test_developer_can_list_own_agents(self, client, db):
        dev = make_user(db, username="dev5", role="developer")
        other_dev = make_user(db, username="dev5b", role="developer")
        make_agent(db, dev, name="agent-a")
        make_agent(db, dev, name="agent-b")
        make_agent(db, other_dev, name="agent-c")
        token = login(client, "dev5")

        resp = client.get("/api/agents",
                          headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        names = {a["name"] for a in resp.json()}
        assert names == {"agent-a", "agent-b"}
        assert "agent-c" not in names

    def test_admin_lists_all_agents(self, client, db):
        dev = make_user(db, username="dev6", role="developer")
        make_user(db, username="admin6", role="admin")
        make_agent(db, dev, name="agent-x")
        make_agent(db, dev, name="agent-y")
        token = login(client, "admin6")

        resp = client.get("/api/agents",
                          headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert len(resp.json()) >= 2

    def test_default_classification_level_put_round_trip(self, client, db):
        """預設分級可經 PUT 寫入並在 list/detail 序列化露出。"""
        dev = make_user(db, username="dev-dcl", role="developer")
        base_model = make_model(db, name="agent-base-dcl")
        agent = make_agent(db, dev, name="dcl-agent")
        agent.base_model_id = base_model.id
        agent.default_classification_level = "無機密"
        agent.classification_ceiling = "極機密"
        db.commit()
        token = login(client, "dev-dcl")

        put = client.put(
            f"/api/agents/{agent.id}",
            json={"default_classification_level": "機密"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert put.status_code == 200, put.text
        assert put.json()["default_classification_level"] == "機密"

        listed = client.get(
            "/api/agents",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert listed.status_code == 200
        row = next(a for a in listed.json() if a["id"] == agent.id)
        assert row["default_classification_level"] == "機密"

        detail = client.get(
            f"/api/agents/{agent.id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert detail.status_code == 200
        assert detail.json()["default_classification_level"] == "機密"
        db.refresh(agent)
        assert agent.default_classification_level == "機密"

    def test_default_above_ceiling_single_field_rejected(self, client, db):
        """單欄更新若使預設分級 > 分類上限 → 422。"""
        dev = make_user(db, username="dev-inv1", role="developer")
        base_model = make_model(db, name="agent-base-inv1")
        agent = make_agent(db, dev, name="inv1-agent")
        agent.base_model_id = base_model.id
        agent.default_classification_level = "無機密"
        agent.classification_ceiling = "營業秘密"
        db.commit()
        token = login(client, "dev-inv1")

        put = client.put(
            f"/api/agents/{agent.id}",
            json={"default_classification_level": "機密"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert put.status_code == 422, put.text
        assert "預設分級不可高於分類上限" in put.json()["detail"]
        db.refresh(agent)
        assert agent.default_classification_level == "無機密"
        assert agent.classification_ceiling == "營業秘密"

    def test_default_above_ceiling_both_fields_rejected(self, client, db):
        """雙欄更新若預設 > 上限 → 422。"""
        dev = make_user(db, username="dev-inv2", role="developer")
        base_model = make_model(db, name="agent-base-inv2")
        agent = make_agent(db, dev, name="inv2-agent")
        agent.base_model_id = base_model.id
        agent.default_classification_level = "無機密"
        agent.classification_ceiling = "極機密"
        db.commit()
        token = login(client, "dev-inv2")

        put = client.put(
            f"/api/agents/{agent.id}",
            json={
                "default_classification_level": "絕對機密",
                "classification_ceiling": "機密",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert put.status_code == 422, put.text
        assert "預設分級不可高於分類上限" in put.json()["detail"]
        db.refresh(agent)
        assert agent.default_classification_level == "無機密"
        assert agent.classification_ceiling == "極機密"

    def test_default_and_ceiling_both_fields_valid(self, client, db):
        """雙欄更新且預設 ≤ 上限 → 200。"""
        dev = make_user(db, username="dev-inv3", role="developer")
        base_model = make_model(db, name="agent-base-inv3")
        agent = make_agent(db, dev, name="inv3-agent")
        agent.base_model_id = base_model.id
        agent.default_classification_level = "無機密"
        agent.classification_ceiling = "極機密"
        db.commit()
        token = login(client, "dev-inv3")

        put = client.put(
            f"/api/agents/{agent.id}",
            json={
                "default_classification_level": "機密",
                "classification_ceiling": "絕對機密",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert put.status_code == 200, put.text
        assert put.json()["default_classification_level"] == "機密"
        assert put.json()["classification_ceiling"] == "絕對機密"
        db.refresh(agent)
        assert agent.default_classification_level == "機密"
        assert agent.classification_ceiling == "絕對機密"


class TestAllowedAgents:
    def test_admin_can_assign_allowed_agents(self, client, db):
        user = make_user(db, username="u1")
        dev = make_user(db, username="dev_aa", role="developer")
        make_user(db, username="admin_aa", role="admin")
        agent = make_agent(db, dev, name="aa-agent", approval_status="approved")
        admin_token = login(client, "admin_aa")

        resp = client.put(
            f"/api/users/{user.id}/allowed-agents",
            json={"agent_ids": [agent.id]},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 200

        resp2 = client.get(
            f"/api/users/{user.id}/allowed-agents",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp2.status_code == 200
        assert any(a["id"] == agent.id for a in resp2.json())

    def test_non_admin_cannot_assign_allowed_agents(self, client, db):
        user = make_user(db, username="u2")
        make_user(db, username="u2b")
        token = login(client, "u2b")

        resp = client.put(
            f"/api/users/{user.id}/allowed-agents",
            json={"agent_ids": []},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403
