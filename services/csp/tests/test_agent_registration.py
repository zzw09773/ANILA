"""Tests for agent registration, approval, rejection, and permission assignment."""

from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest

from tests.conftest import make_user, make_agent, make_model, login


@pytest.fixture(autouse=True)
def _env_allowances(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "agent,mock-llm")


class TestAgentRegistration:
    def test_developer_can_register_agent(self, client, db):
        make_user(db, username="dev1", role="developer")
        model = make_model(db, name="reg-model-1")
        token = login(client, "dev1")

        resp = client.post(
            "/api/agents/register",
            json={
                "name": "my-agent",
                "endpoint_url": "http://agent:9100",
                "description_for_router": "Handles HR queries",
                "base_model_id": model.id,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "my-agent"
        assert data["approval_status"] == "registered"
        from app.models.user import User
        owner = db.query(User).filter(User.username == "dev1").first()
        assert data["owner_user_id"] == owner.id

    def test_plain_user_cannot_register(self, client, db):
        make_user(db, username="user1", role="user")
        token = login(client, "user1")

        resp = client.post(
            "/api/agents/register",
            json={
                "name": "sneaky-agent",
                "endpoint_url": "http://x:9100",
                "description_for_router": "...",
                "base_model_id": 1,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    def test_duplicate_name_rejected(self, client, db):
        dev = make_user(db, username="dev2", role="developer")
        make_agent(db, dev, name="dup-agent")
        model = make_model(db, name="dup-model")
        token = login(client, "dev2")

        resp = client.post(
            "/api/agents/register",
            json={
                "name": "dup-agent",
                "endpoint_url": "http://agent:9100",
                "description_for_router": "...",
                "base_model_id": model.id,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400

    def test_admin_can_approve_registered(self, client, db):
        """OE-1: no trace-test gate — registered alone is enough."""
        dev = make_user(db, username="dev3", role="developer")
        admin = make_user(db, username="admin3", role="admin")
        agent = make_agent(db, dev, name="pending-agent", approval_status="registered")
        token = login(client, "admin3")

        resp = client.post(
            f"/api/agents/{agent.id}/approve",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        db.refresh(agent)
        assert agent.approval_status == "approved"
        assert agent.approved_by == admin.id

    def test_admin_can_reject_to_disabled(self, client, db):
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
        assert agent.approval_status == "disabled"

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

    def test_assign_registered_auto_approves(self, client, db):
        """SYSTEM-MAP: register → admin assigns → usable (no other gate)."""
        user = make_user(db, username="u_reg")
        dev = make_user(db, username="dev_reg", role="developer")
        make_user(db, username="admin_reg", role="admin")
        agent = make_agent(db, dev, name="reg-agent", approval_status="registered")
        admin_token = login(client, "admin_reg")

        resp = client.put(
            f"/api/users/{user.id}/allowed-agents",
            json={"agent_ids": [agent.id]},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 200, resp.text
        db.refresh(agent)
        assert agent.approval_status == "approved"

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
