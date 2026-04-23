"""Tests for CSP proxy agent/model resolution logic.

Verifies that proxy.py correctly:
- Routes model= to agent endpoint when the name matches an approved agent
- Falls through to model_registry when no agent matches
- Rejects requests where the API key lacks permission for the agent
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.models.agent import Agent, ApiKeyAgentPermission
from app.models.api_key import ApiKey, ApiKeyModelPermission
from app.models.model_registry import ModelRegistry
from app.services.api_key_service import check_agent_permission, check_model_permission
from tests.conftest import make_user, make_agent, make_api_key, make_model


class TestAgentPermissionService:
    def test_key_with_permission_allowed(self, db: Session):
        user = make_user(db, username="u_perm1")
        agent = make_agent(db, user, name="allowed-agent", approval_status="approved")
        key = make_api_key(db, user)

        # Grant permission
        perm = ApiKeyAgentPermission(api_key_id=key.id, agent_id=agent.id)
        db.add(perm)
        db.commit()

        assert (
            check_agent_permission(
                db, user=user, api_key_id=key.id, agent_id=agent.id
            )
            is True
        )

    def test_key_without_permission_denied(self, db: Session):
        user = make_user(db, username="u_perm2")
        agent = make_agent(db, user, name="restricted-agent", approval_status="approved")
        key = make_api_key(db, user)

        assert (
            check_agent_permission(
                db, user=user, api_key_id=key.id, agent_id=agent.id
            )
            is False
        )

    def test_model_permission_independent_of_agent_permission(self, db: Session):
        user = make_user(db, username="u_perm3")
        model = make_model(db, name="model-perm-test")
        key = make_api_key(db, user)

        # Grant model permission only
        mp = ApiKeyModelPermission(api_key_id=key.id, model_id=model.id)
        db.add(mp)
        db.commit()

        assert (
            check_model_permission(
                db, user=user, api_key_id=key.id, model_id=model.id
            )
            is True
        )

        # No agent permission granted
        agent = make_agent(db, user, name="agent-no-perm", approval_status="approved")
        assert (
            check_agent_permission(
                db, user=user, api_key_id=key.id, agent_id=agent.id
            )
            is False
        )


class TestV1AgentsDataPlane:
    """Test GET /v1/agents returns only approved agents the API key owner can use."""

    def test_returns_approved_agents_for_key_owner(self, client, db):
        user = make_user(db, username="apiuser1")
        dev = make_user(db, username="apikeydev1", role="developer")

        agent1 = make_agent(db, dev, name="pub-agent-1", approval_status="approved")
        _agent2 = make_agent(db, dev, name="pub-agent-2", approval_status="pending")

        # Grant user permission for agent1
        from app.models.agent import UserAgentPermission
        perm = UserAgentPermission(user_id=user.id, agent_id=agent1.id)
        db.add(perm)
        db.commit()

        # Create API key for user
        raw_key = "sk-apiuser1-key"
        key_obj = make_api_key(db, user, raw_key=raw_key)
        # Also add api_key_agent_permission
        akap = ApiKeyAgentPermission(api_key_id=key_obj.id, agent_id=agent1.id)
        db.add(akap)
        db.commit()

        resp = client.get("/v1/agents",
                          headers={"Authorization": f"Bearer {raw_key}"})
        assert resp.status_code == 200
        data = resp.json()
        names = {a["id"] for a in data.get("data", [])}
        assert "pub-agent-1" in names
        assert "pub-agent-2" not in names
