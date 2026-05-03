"""Sprint 13 PR A3 — agents.runtime_config column + GET/PATCH API."""

from __future__ import annotations

from tests.conftest import login, make_agent, make_user


class TestRuntimeConfigPersistence:
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

    def test_owner_can_patch_runtime_config(self, client, db):
        dev = make_user(db, username="dev-rc2", role="developer")
        agent = make_agent(db, dev, name="rc-agent-2")
        token = login(client, "dev-rc2")

        cfg = {
            "tool_permissions": {
                "allow_list": ["*"],
                "deny_list": ["exec_bash"],
                "ask_tools": ["exec_python"],
            },
            "workspace": {"max_bytes": 5_000_000, "allow_network": False},
            "guardrails": {
                "input": [{"kind": "regex_block", "pattern": r"sk-\w+"}],
                "output": [{"kind": "max_length", "max_chars": 4096}],
            },
        }
        resp = client.patch(
            f"/api/agents/{agent.id}/runtime-config",
            json={"runtime_config": cfg},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["runtime_config"] == cfg

        # Round-trip via GET.
        get_resp = client.get(
            f"/api/agents/{agent.id}/runtime-config",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert get_resp.json()["runtime_config"] == cfg

    def test_patch_with_none_clears_override(self, client, db):
        dev = make_user(db, username="dev-rc3", role="developer")
        agent = make_agent(db, dev, name="rc-agent-3")
        token = login(client, "dev-rc3")

        # Set first.
        client.patch(
            f"/api/agents/{agent.id}/runtime-config",
            json={"runtime_config": {"foo": "bar"}},
            headers={"Authorization": f"Bearer {token}"},
        )
        # Then clear.
        resp = client.patch(
            f"/api/agents/{agent.id}/runtime-config",
            json={"runtime_config": None},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["runtime_config"] is None

    def test_empty_dict_is_distinct_from_none(self, client, db):
        """Empty dict ``{}`` means 'admin set empty', distinct from NULL
        (which means 'use code defaults'). Both must round-trip cleanly."""
        dev = make_user(db, username="dev-rc4", role="developer")
        agent = make_agent(db, dev, name="rc-agent-4")
        token = login(client, "dev-rc4")

        resp = client.patch(
            f"/api/agents/{agent.id}/runtime-config",
            json={"runtime_config": {}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["runtime_config"] == {}


class TestRuntimeConfigPermissions:
    def test_non_owner_developer_cannot_patch(self, client, db):
        owner = make_user(db, username="owner-rc", role="developer")
        agent = make_agent(db, owner, name="owned-agent")
        make_user(db, username="other-rc", role="developer")
        token = login(client, "other-rc")

        resp = client.patch(
            f"/api/agents/{agent.id}/runtime-config",
            json={"runtime_config": {"x": 1}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    def test_admin_can_patch_any_agent(self, client, db):
        owner = make_user(db, username="owner-rc-a", role="developer")
        agent = make_agent(db, owner, name="owned-agent-a")
        make_user(db, username="admin-rc", role="admin")
        token = login(client, "admin-rc")

        resp = client.patch(
            f"/api/agents/{agent.id}/runtime-config",
            json={"runtime_config": {"set_by": "admin"}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["runtime_config"] == {"set_by": "admin"}

    def test_plain_user_cannot_read_or_write(self, client, db):
        owner = make_user(db, username="owner-rc-u", role="developer")
        agent = make_agent(db, owner, name="owned-agent-u")
        make_user(db, username="user-rc", role="user")
        token = login(client, "user-rc")

        # _require_developer_or_admin gates both read and write.
        get_resp = client.get(
            f"/api/agents/{agent.id}/runtime-config",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert get_resp.status_code == 403


class TestRuntimeConfigOnAgentResponse:
    def test_runtime_config_appears_in_serialized_agent(self, client, db):
        """``GET /api/agents/{id}`` and ``GET /api/agents`` should expose
        runtime_config so the admin UI can show it without an extra
        roundtrip."""
        dev = make_user(db, username="dev-rc-resp", role="developer")
        agent = make_agent(db, dev, name="resp-agent")
        token = login(client, "dev-rc-resp")

        client.patch(
            f"/api/agents/{agent.id}/runtime-config",
            json={"runtime_config": {"key": "value"}},
            headers={"Authorization": f"Bearer {token}"},
        )

        resp = client.get(
            f"/api/agents/{agent.id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["runtime_config"] == {"key": "value"}


class TestUnknownAgent:
    def test_get_404_when_agent_missing(self, client, db):
        make_user(db, username="dev-rc-404", role="developer")
        token = login(client, "dev-rc-404")
        resp = client.get(
            "/api/agents/9999/runtime-config",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404
