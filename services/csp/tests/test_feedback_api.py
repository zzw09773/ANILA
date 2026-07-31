"""P3.4 使用者回饋總覽 —— 授權、篩選、密等不洩訊息正文。"""

from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.api.admin.feedback import FEEDBACK_ITEM_KEYS
from app.models.conversation import Conversation
from app.models.message import Message
from app.services.auth_service import create_tokens
from tests.conftest import make_user

FEEDBACK_URL = "/api/admin/feedback"

SECRET_PAYLOAD = "CLASSIFIED-BODY-SHOULD-NEVER-APPEAR-IN-FEEDBACK-LIST"


def _bearer(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_tokens(user)['access_token']}"}


def _seed_rated_message(
    db: Session,
    *,
    owner,
    rating: str = "down",
    content: str = "assistant reply",
    classification_level: str = "無機密",
    model_name: str = "gpt-oss",
    agent_name: str = "briefing-agent",
    comment: str | None = "答非所問",
    reasons: list | None = None,
) -> Message:
    conv = Conversation(
        user_id=owner.id,
        title="feedback-seed",
        classification_level=classification_level,
    )
    db.add(conv)
    db.flush()
    meta = None
    if comment is not None or reasons:
        meta = {
            "feedback": {
                "comment": comment,
                "reasons": reasons or [],
            }
        }
    msg = Message(
        conversation_id=conv.id,
        role="assistant",
        content=content,
        rating=rating,
        model_name=model_name,
        agent_name=agent_name,
        classification_level=classification_level,
        metadata_=meta,
        created_at=datetime.now(timezone.utc),
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


def test_feedback_requires_authentication(client):
    assert client.get(FEEDBACK_URL).status_code == 401


@pytest.mark.parametrize("role", ["user", "developer"])
def test_feedback_rejects_non_admin(client, db: Session, role: str):
    user = make_user(db, username=f"fb-{role}", role=role)
    assert client.get(FEEDBACK_URL, headers=_bearer(user)).status_code == 403


def test_feedback_admin_sees_rating_and_comment(client, db: Session):
    admin = make_user(db, username="fb-admin", role="admin")
    owner = make_user(db, username="fb-owner-user", role="user")
    _seed_rated_message(db, owner=owner, rating="down", comment="亂講")

    resp = client.get(FEEDBACK_URL, headers=_bearer(admin))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["summary"]["down"] >= 1
    assert body["items"]
    item = body["items"][0]
    assert item["rating"] == "down"
    assert item["comment"] == "亂講"
    assert item["agent_name"] == "briefing-agent"
    assert item["model_name"] == "gpt-oss"


def test_feedback_owner_also_allowed(client, db: Session):
    owner = make_user(db, username="fb-platform-owner", role="owner")
    user = make_user(db, username="fb-u2", role="user")
    _seed_rated_message(db, owner=user, rating="up", comment=None)

    assert client.get(FEEDBACK_URL, headers=_bearer(owner)).status_code == 200


def test_feedback_filter_by_rating_and_agent(client, db: Session):
    admin = make_user(db, username="fb-filter-admin", role="admin")
    owner = make_user(db, username="fb-filter-u", role="user")
    _seed_rated_message(
        db, owner=owner, rating="down", agent_name="agent-a", comment="a"
    )
    _seed_rated_message(
        db, owner=owner, rating="up", agent_name="agent-b", comment=None
    )

    resp = client.get(
        FEEDBACK_URL,
        headers=_bearer(admin),
        params={"rating": "down", "agent_name": "agent-a"},
    )

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items
    assert all(i["rating"] == "down" and i["agent_name"] == "agent-a" for i in items)


def test_feedback_never_returns_message_content_even_for_secret(
    client, db: Session
):
    """Classification rule: list must not become a bulk read of controlled text.

    Existing conversation GET lets admin-tier read bodies (with audit ≥營業秘密).
    This list endpoint must never include ``content``, including when the row
    is 機密 and the caller is admin — otherwise the governance console becomes
    a side channel past the audited read path.
    """
    admin = make_user(db, username="fb-secret-admin", role="admin")
    owner = make_user(db, username="fb-secret-u", role="user")
    _seed_rated_message(
        db,
        owner=owner,
        rating="down",
        content=SECRET_PAYLOAD,
        classification_level="機密",
        comment="仍看得到留言",
    )

    resp = client.get(FEEDBACK_URL, headers=_bearer(admin))

    assert resp.status_code == 200, resp.text
    raw = resp.text
    assert SECRET_PAYLOAD not in raw
    body = resp.json()
    assert body["items"]
    item = body["items"][0]
    assert "content" not in item
    assert set(item.keys()) == FEEDBACK_ITEM_KEYS
    assert item["classification_level"] == "機密"
    assert item["comment"] == "仍看得到留言"


def test_feedback_response_fields_are_exactly_the_whitelist(client, db: Session):
    admin = make_user(db, username="fb-wl-admin", role="admin")
    owner = make_user(db, username="fb-wl-u", role="user")
    _seed_rated_message(db, owner=owner)

    body = client.get(FEEDBACK_URL, headers=_bearer(admin)).json()

    assert set(body.keys()) == {"summary", "items"}
    assert set(body["summary"].keys()) == {"total", "up", "down", "with_comment"}
    for item in body["items"]:
        assert set(item.keys()) == FEEDBACK_ITEM_KEYS
        assert "content" not in item
