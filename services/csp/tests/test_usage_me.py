# -*- coding: utf-8 -*-
"""B 期：一般使用者自己的用量端點。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.token_usage import TokenUsage
from tests.conftest import login, make_model, make_user


def _add_usage(
    db: Session,
    *,
    user,
    model,
    prompt: int,
    completion: int,
    reasoning: int | None = None,
    request_type: str = "chat",
    when: datetime | None = None,
    conversation_id: str | None = None,
):
    row = TokenUsage(
        api_key_id=None,
        user_id=user.id,
        model_id=model.id,
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        reasoning_tokens=reasoning,
        request_timestamp=when or datetime.now(timezone.utc),
        request_type=request_type,
        conversation_id=conversation_id,
        token_source="reported",
        model_name_snapshot=model.name,
    )
    db.add(row)
    db.commit()
    return row


def test_usage_me_is_self_only(client, db: Session):
    model_a = make_model(db, name="me-model-a")
    model_b = make_model(db, name="me-model-b")
    user_a = make_user(db, username="usage-me-a")
    user_b = make_user(db, username="usage-me-b")
    now = datetime.now(timezone.utc)
    _add_usage(db, user=user_a, model=model_a, prompt=10, completion=20, reasoning=7, when=now)
    _add_usage(db, user=user_a, model=model_b, prompt=3, completion=4, reasoning=1, when=now)
    _add_usage(
        db,
        user=user_a,
        model=model_a,
        prompt=5,
        completion=0,
        reasoning=None,
        request_type="embedding",
        when=now,
    )
    _add_usage(db, user=user_b, model=model_a, prompt=100, completion=200, reasoning=50, when=now)

    headers = {"Authorization": f"Bearer {login(client, 'usage-me-a')}"}
    resp = client.get("/api/usage/me?range=7d", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["range"] == "7d"
    assert body["requests"] == 3
    assert body["prompt_tokens"] == 18
    assert body["completion_tokens"] == 24
    assert body["reasoning_tokens"] == 8
    assert body["total_tokens"] == 42
    assert "api_key" not in body
    assert not any("api_key" in item for item in body["by_model"])

    by_model = {item["model_name"]: item for item in body["by_model"]}
    assert by_model["me-model-a"]["requests"] == 2
    assert by_model["me-model-a"]["prompt_tokens"] == 15
    assert by_model["me-model-a"]["completion_tokens"] == 20
    assert by_model["me-model-a"]["reasoning_tokens"] == 7
    assert by_model["me-model-b"]["requests"] == 1
    assert by_model["me-model-b"]["prompt_tokens"] == 3

    by_kind = {item["request_type"]: item for item in body["by_kind"]}
    assert by_kind["chat"]["requests"] == 2
    assert by_kind["embedding"]["requests"] == 1
    assert by_kind["embedding"]["prompt_tokens"] == 5

    today = (now.astimezone(timezone(timedelta(hours=8)))).date().isoformat()
    by_day = {item["date"]: item for item in body["by_day"]}
    assert by_day[today]["prompt_tokens"] == 18
    assert by_day[today]["completion_tokens"] == 24
    assert by_day[today]["reasoning_tokens"] == 8


def test_usage_me_range_filters_old_rows(client, db: Session):
    model = make_model(db, name="me-range-model")
    user = make_user(db, username="usage-me-range")
    now = datetime.now(timezone.utc)
    _add_usage(db, user=user, model=model, prompt=2, completion=2, reasoning=1, when=now)
    _add_usage(
        db,
        user=user,
        model=model,
        prompt=9,
        completion=9,
        reasoning=9,
        when=now - timedelta(days=2),
    )
    headers = {"Authorization": f"Bearer {login(client, 'usage-me-range')}"}
    day = client.get("/api/usage/me?range=24h", headers=headers)
    assert day.status_code == 200, day.text
    assert day.json()["requests"] == 1
    assert day.json()["prompt_tokens"] == 2
    week = client.get("/api/usage/me?range=7d", headers=headers)
    assert week.status_code == 200, week.text
    assert week.json()["requests"] == 2
    assert week.json()["prompt_tokens"] == 11
    assert len(week.json()["by_day"]) == 2


def test_usage_me_unauthenticated_401(client, db: Session):
    resp = client.get("/api/usage/me?range=24h")
    assert resp.status_code == 401


def test_conversation_usage_owner_only(client, db: Session):
    model = make_model(db, name="conv-usage-model")
    owner = make_user(db, username="conv-usage-owner")
    other = make_user(db, username="conv-usage-other")
    owner_headers = {"Authorization": f"Bearer {login(client, 'conv-usage-owner')}"}
    other_headers = {"Authorization": f"Bearer {login(client, 'conv-usage-other')}"}
    created = client.post(
        "/api/conversations",
        headers=owner_headers,
        json={"title": "usage", "origin": "anila-ui"},
    )
    assert created.status_code == 201, created.text
    conv_id = created.json()["id"]
    _add_usage(
        db,
        user=owner,
        model=model,
        prompt=6,
        completion=8,
        reasoning=12,
        conversation_id=str(conv_id),
    )
    _add_usage(
        db,
        user=owner,
        model=model,
        prompt=1,
        completion=1,
        reasoning=None,
        conversation_id=str(conv_id),
    )
    _add_usage(
        db,
        user=other,
        model=model,
        prompt=99,
        completion=99,
        reasoning=99,
        conversation_id=str(conv_id),
    )

    mine = client.get(f"/api/conversations/{conv_id}/usage", headers=owner_headers)
    assert mine.status_code == 200, mine.text
    body = mine.json()
    assert body["conversation_id"] == str(conv_id)
    assert body["requests"] == 2
    assert body["prompt_tokens"] == 7
    assert body["completion_tokens"] == 9
    assert body["reasoning_tokens"] == 12
    assert body["total_tokens"] == 16
    assert "api_key" not in body

    foreign = client.get(f"/api/conversations/{conv_id}/usage", headers=other_headers)
    assert foreign.status_code == 404
