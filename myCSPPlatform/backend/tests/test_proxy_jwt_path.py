"""End-to-end tests for the JWT path through ``/v1/*`` endpoints.

Validates Wave 1 of the data-plane auth unification:
- SPA / web sessions authenticated via JWT can call ``/v1/agents`` and
  ``/v1/chat/completions`` without holding an API key.
- Agent permission is checked against ``UserAgentPermission`` on the JWT
  path (no ``ApiKeyAgentPermission`` exists for JWT traffic).
- Usage rows written from a JWT-authenticated call carry
  ``api_key_id IS NULL`` and are counted in the dashboard's "web_ui_requests"
  bucket.
- The legacy API-key path keeps working (regression).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.agent import Agent, UserAgentPermission
from app.models.token_usage import TokenUsage
from app.services.auth_service import create_tokens
from app.services.usage_service import get_usage_summary

from tests.conftest import make_agent, make_api_key, make_model, make_user


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _jwt_access(user) -> str:
    return create_tokens(user)["access_token"]


def test_v1_agents_via_jwt_returns_permitted_agents(client: TestClient, db: Session):
    user = make_user(db, username="spa_user")
    dev = make_user(db, username="spa_dev", role="developer")
    ag = make_agent(db, dev, name="spa-agent", approval_status="approved")

    db.add(UserAgentPermission(user_id=user.id, agent_id=ag.id))
    db.commit()

    resp = client.get("/v1/agents", headers=_bearer(_jwt_access(user)))
    assert resp.status_code == 200, resp.text
    names = {a["id"] for a in resp.json()["data"]}
    assert "spa-agent" in names


def test_v1_agents_via_jwt_excludes_unapproved(client: TestClient, db: Session):
    user = make_user(db, username="spa_user2")
    dev = make_user(db, username="spa_dev2", role="developer")
    ag = make_agent(db, dev, name="not-yet-approved", approval_status="pending")
    db.add(UserAgentPermission(user_id=user.id, agent_id=ag.id))
    db.commit()

    resp = client.get("/v1/agents", headers=_bearer(_jwt_access(user)))
    assert resp.status_code == 200
    names = {a["id"] for a in resp.json()["data"]}
    assert "not-yet-approved" not in names


def test_chat_completions_via_jwt_denies_when_no_agent_permission(
    client: TestClient, db: Session
):
    """JWT path must still honor UserAgentPermission; no permission → 403."""
    user = make_user(db, username="spa_denied")
    dev = make_user(db, username="spa_denied_dev", role="developer")
    make_agent(db, dev, name="locked-agent", approval_status="approved")

    resp = client.post(
        "/v1/chat/completions",
        headers=_bearer(_jwt_access(user)),
        json={"model": "locked-agent", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 403
    assert "無權" in resp.json()["detail"]


def test_chat_completions_via_jwt_denies_model_without_permission(
    client: TestClient, db: Session
):
    user = make_user(db, username="spa_denied_model")
    make_model(db, name="restricted-llm")

    resp = client.post(
        "/v1/chat/completions",
        headers=_bearer(_jwt_access(user)),
        json={"model": "restricted-llm", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 403


def test_chat_completions_via_jwt_allows_admin_on_any_agent(
    client: TestClient, db: Session, monkeypatch
):
    """Admin role bypasses per-user/per-key checks on both paths."""
    admin = make_user(db, username="spa_admin", role="admin")
    dev = make_user(db, username="spa_admin_dev", role="developer")
    ag = make_agent(db, dev, name="admin-viewable-agent", approval_status="approved")

    # Don't grant any permission — admin should still resolve the agent
    # (we only assert resolution, not a real chat call, to keep the test
    # hermetic without standing up a downstream LLM mock).
    from app.api.proxy import _resolve_agent
    from app.middleware.caller import Caller

    caller = Caller(user=admin, api_key_id=None)
    resolved = _resolve_agent(db, caller, "admin-viewable-agent")
    assert resolved is not None
    assert resolved.id == ag.id


def test_api_key_path_still_works_regression(client: TestClient, db: Session):
    """sk-* path must remain functional after the refactor."""
    user = make_user(db, username="sdk_user")
    dev = make_user(db, username="sdk_dev", role="developer")
    ag = make_agent(db, dev, name="sdk-agent", approval_status="approved")

    db.add(UserAgentPermission(user_id=user.id, agent_id=ag.id))
    db.commit()

    raw_key = "sk-regression-test-key"
    make_api_key(db, user, raw_key=raw_key)

    resp = client.get("/v1/agents", headers=_bearer(raw_key))
    assert resp.status_code == 200
    names = {a["id"] for a in resp.json()["data"]}
    assert "sdk-agent" in names


def test_usage_summary_counts_web_ui_bucket(db: Session):
    """JWT-attributed TokenUsage rows (api_key_id IS NULL) roll into
    ``web_ui_requests``; they do NOT inflate ``active_api_keys``."""
    user = make_user(db, username="usage_user")
    model = make_model(db, name="usage-model")

    # Two JWT-attributed rows (no api_key_id) and one API-key-attributed row.
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    key = make_api_key(db, user)
    db.add(
        TokenUsage(
            api_key_id=None,
            user_id=user.id,
            model_id=model.id,
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            request_timestamp=now,
        )
    )
    db.add(
        TokenUsage(
            api_key_id=None,
            user_id=user.id,
            model_id=model.id,
            prompt_tokens=5,
            completion_tokens=5,
            total_tokens=10,
            request_timestamp=now,
        )
    )
    db.add(
        TokenUsage(
            api_key_id=key.id,
            user_id=user.id,
            model_id=model.id,
            prompt_tokens=1,
            completion_tokens=2,
            total_tokens=3,
            request_timestamp=now,
        )
    )
    db.commit()

    summary = get_usage_summary(db, range_key="24h", user_id=user.id)
    assert summary["total_requests"] == 3
    assert summary["web_ui_requests"] == 2
    assert summary["active_api_keys"] == 1  # NULLs skipped by COUNT(DISTINCT)
