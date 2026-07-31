"""Compare-mode adopt — promote on-screen answer into a real conversation.

Pins: message tree persistence, agent-policy classification latch, and
visible failure (no silent local invent). Mutant notes below say what to
revert to turn each assertion red.
"""
from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.conversation import Conversation
from app.models.message import Message
from app.models.agent import UserAgentPermission
from app.models.user import User
from tests.conftest import login, make_agent, make_user


@pytest.fixture(autouse=True)
def _bypass_dev_secret_gate(monkeypatch):
    import app.services.startup_security as ss_module

    monkeypatch.setattr(ss_module, "assert_no_dev_defaults", lambda: None)


def _auth(client: TestClient, db: Session, username: str = "adopt_user"):
    user = make_user(db, username=username, role="user")
    token = login(client, username=username)
    return user, {"Authorization": f"Bearer {token}"}


def test_adopt_persists_tree_and_survives_reload(client: TestClient, db: Session):
    """Mutant: delete message appends in adopt_compare_answer → messages empty."""
    _, h = _auth(client, db, "adopt_persist")
    resp = client.post(
        "/api/conversations/adopt",
        json={
            "title": "採用比較結果",
            "user_content": "同一問題",
            "assistant_content": "被採用的回答",
            "origin": "anila-ui",
        },
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    detail = resp.json()
    cid = detail["id"]
    assert isinstance(cid, int)
    assert len(detail["messages"]) == 2
    assert detail["messages"][0]["role"] == "user"
    assert detail["messages"][0]["content"] == "同一問題"
    assert detail["messages"][1]["role"] == "assistant"
    assert detail["messages"][1]["content"] == "被採用的回答"
    assert detail["messages"][1]["parent_id"] == detail["messages"][0]["id"]
    assert detail["active_leaf_message_id"] == detail["messages"][1]["id"]

    reload = client.get(f"/api/conversations/{cid}?view=active", headers=h)
    assert reload.status_code == 200, reload.text
    body = reload.json()
    assert [m["content"] for m in body["messages"]] == [
        "同一問題",
        "被採用的回答",
    ]


def test_adopt_latches_from_agent_policy(client: TestClient, db: Session):
    """Mutant: skip _latch_agent_policy_on_conversation → classified stays false."""
    user, h = _auth(client, db, "adopt_latch")
    agent = make_agent(db, user, name="secret-compare-agent", approval_status="approved")
    agent.requires_encryption = True
    agent.default_classification_level = "密"
    db.add(UserAgentPermission(user_id=user.id, agent_id=agent.id))
    db.commit()

    resp = client.post(
        "/api/conversations/adopt",
        json={
            "title": "密等採用",
            "agent_name": "secret-compare-agent",
            "user_content": "密等問題",
            "assistant_content": "密等回答",
            "origin": "anila-ui",
        },
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    detail = resp.json()
    # Proven by API response — not by what the client sent.
    assert detail["classified"] is True
    assert detail["classification_level"] == "密"

    # Hard-reload path: GET must still show the latch.
    reload = client.get(
        f"/api/conversations/{detail['id']}?view=active", headers=h,
    )
    assert reload.status_code == 200
    assert reload.json()["classified"] is True
    assert reload.json()["classification_level"] == "密"

    row = db.get(Conversation, detail["id"])
    assert row is not None
    assert row.agent_id == agent.id


def test_adopt_rejects_empty_user(client: TestClient, db: Session):
    """Mutant: drop the empty-user guard → 201 with blank user content."""
    _, h = _auth(client, db, "adopt_empty")
    resp = client.post(
        "/api/conversations/adopt",
        json={
            "user_content": "   ",
            "assistant_content": "x",
        },
        headers=h,
    )
    assert resp.status_code == 400
    assert "使用者" in resp.json()["detail"]


def test_adopt_does_not_latch_without_agent_policy(
    client: TestClient, db: Session,
):
    """Unclassified agent → conversation stays unlatched (server decides)."""
    user, h = _auth(client, db, "adopt_plain")
    agent = make_agent(db, user, name="plain-agent", approval_status="approved")
    db.add(UserAgentPermission(user_id=user.id, agent_id=agent.id))
    db.commit()
    resp = client.post(
        "/api/conversations/adopt",
        json={
            "agent_name": "plain-agent",
            "user_content": "Q",
            "assistant_content": "A",
        },
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    detail = resp.json()
    assert detail["classified"] is False
    assert detail["classification_level"] in ("無機密", None)
