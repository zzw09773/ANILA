"""Agent runtime_config: GET stays; PATCH retired (official agent never polls)."""

from __future__ import annotations

from tests.conftest import login, make_agent, make_user


class TestRuntimeConfigRead:
    def test_default_runtime_config_is_none(self, client, db):
        dev = make_user(db, username="dev-rc1", role="developer")
        agent = make_agent(db, dev, name="rc-agent-1")
        token = login(client, "dev-rc1")

        resp = client.get(
            f"/api/agents/{agent.id}/runtime-config",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["agent_id"] == agent.id
        assert body["agent_name"] == "rc-agent-1"
        assert body["runtime_config"] is None

    def test_get_404_when_agent_missing(self, client, db):
        make_user(db, username="dev-rc-404", role="developer")
        token = login(client, "dev-rc-404")
        resp = client.get(
            "/api/agents/9999/runtime-config",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    def test_plain_user_cannot_read(self, client, db):
        owner = make_user(db, username="owner-rc-u", role="developer")
        agent = make_agent(db, owner, name="owned-agent-u")
        make_user(db, username="user-rc", role="user")
        token = login(client, "user-rc")

        get_resp = client.get(
            f"/api/agents/{agent.id}/runtime-config",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert get_resp.status_code == 403


class TestRuntimeConfigWriteRetired:
    """Revert patch_agent_runtime_config to the old write body → these go red."""

    def test_owner_patch_returns_410(self, client, db):
        dev = make_user(db, username="dev-rc2", role="developer")
        agent = make_agent(db, dev, name="rc-agent-2")
        token = login(client, "dev-rc2")

        resp = client.patch(
            f"/api/agents/{agent.id}/runtime-config",
            json={"runtime_config": {"tool_permissions": {"deny_list": ["x"]}}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 410
        assert "runtime_config" in resp.json()["detail"]

    def test_admin_patch_returns_410(self, client, db):
        owner = make_user(db, username="owner-rc-a", role="developer")
        agent = make_agent(db, owner, name="owned-agent-a")
        make_user(db, username="admin-rc", role="admin")
        token = login(client, "admin-rc")

        resp = client.patch(
            f"/api/agents/{agent.id}/runtime-config",
            json={"runtime_config": {"set_by": "admin"}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 410

    def test_serialized_agent_still_exposes_stored_null(self, client, db):
        """GET agent still includes runtime_config key (read-only; usually null)."""
        dev = make_user(db, username="dev-rc-resp", role="developer")
        agent = make_agent(db, dev, name="resp-agent")
        token = login(client, "dev-rc-resp")

        resp = client.get(
            f"/api/agents/{agent.id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert "runtime_config" in resp.json()
        assert resp.json()["runtime_config"] is None
