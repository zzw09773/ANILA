"""Tests for agent registration, approval, rejection, and permission assignment."""

from __future__ import annotations

import pytest
from tests.conftest import make_user, make_agent, login


class TestAgentRegistration:
    def test_developer_can_register_agent(self, client, db):
        dev = make_user(db, username="dev1", role="developer")
        token = login(client, "dev1")

        resp = client.post(
            "/api/agents/register",
            json={
                "name": "my-agent",
                "endpoint_url": "http://agent:9100",
                "description_for_router": "Handles HR queries",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "my-agent"
        assert data["approval_status"] == "pending"
        assert data["owner_user_id"] == dev.id

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
        make_agent(db, dev, name="dup-agent")
        token = login(client, "dev2")

        resp = client.post(
            "/api/agents/register",
            json={
                "name": "dup-agent",
                "endpoint_url": "http://agent:9100",
                "description_for_router": "...",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400

    def test_admin_can_approve(self, client, db):
        dev = make_user(db, username="dev3", role="developer")
        admin = make_user(db, username="admin3", role="admin")
        agent = make_agent(db, dev, name="pending-agent")
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


class TestAllowedAgents:
    def test_admin_can_assign_allowed_agents(self, client, db):
        user = make_user(db, username="u1")
        dev = make_user(db, username="dev_aa", role="developer")
        make_user(db, username="admin_aa", role="admin")
        agent = make_agent(db, dev, name="aa-agent", approval_status="approved")
        admin_token = login(client, "admin_aa")

        resp = client.put(
            f"/api/users/{user.id}/allowed-agents",
            json=[agent.id],
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
            json=[],
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403
