"""Agent 底層模型代理：派工 JWT 呼叫模型、用量歸提問者、模型下線。

設計：docs/designs/agent-model-delegation-2026-09-25.md 第 6 節。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from jose import jwt
from sqlalchemy.orm import Session, sessionmaker

from app.api import proxy as proxy_api
from app.config import settings
from app.middleware.caller import Caller
from app.models.agent import Agent, UserAgentPermission
from app.models.department import Department
from app.models.handoff import Notification
from app.models.token_usage import TokenUsage
from app.models.user import UserModelPermission
from app.services import proxy_service, usage_writer
from app.services.api_key_service import check_model_permission
from app.services.auth_service import create_tokens
from app.services.proxy.dispatch_token import (
    build_dispatch_claims,
    issue_dispatch_token,
)
from app.services.usage_service import get_top_agents
from app.utils.security import ALGORITHM, get_private_key
from tests.conftest import login, make_agent, make_model, make_user

UNAVAILABLE = "此助手暫時無法使用"


class _PostResponse:
    status_code = 200
    headers = {"content-type": "application/json"}

    def __init__(self):
        self.text = json.dumps(
            {
                "choices": [
                    {"message": {"role": "assistant", "content": "answer"}}
                ],
                "usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 11,
                    "total_tokens": 18,
                },
            }
        )

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        return None


class _RecordingClient:
    posts: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).posts.append({"url": url, "json": json, "headers": headers})
        return _PostResponse()


class _FakeStreamResponse:
    def __init__(self, lines: list[str]):
        self._lines = lines
        self.status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _StreamClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method, url, json=None, headers=None):
        lines = [
            'data: {"choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}',
            "",
            'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":4,"completion_tokens":2,"total_tokens":6}}',
            "",
            "data: [DONE]",
            "",
        ]
        return _FakeStreamResponse(lines)


class _Request:
    def __init__(self, body: dict, headers: dict | None = None):
        self._body = body
        self.headers = headers or {}

    async def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _allow_mock_upstream(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "agent,mock-llm")
    _RecordingClient.posts = []
    monkeypatch.setattr(
        proxy_service.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _RecordingClient(*args, **kwargs),
    )


@pytest.fixture
def usage_queue(db_engine, monkeypatch):
    monkeypatch.setattr(
        usage_writer,
        "SessionLocal",
        sessionmaker(bind=db_engine, expire_on_commit=False),
    )
    monkeypatch.setattr(usage_writer, "_usage_queue", None)
    return usage_writer.get_usage_queue()


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _department(db: Session, name: str = "人事處") -> Department:
    row = Department(name=name, is_active=True)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _signed(claims: dict) -> str:
    return jwt.encode(
        claims,
        get_private_key(),
        algorithm=ALGORITHM,
        headers={"kid": settings.JWT_KID, "typ": "JWT"},
    )


def _approved_agent(db: Session, *, owner: User, name: str, base):
    agent = make_agent(db, owner, name=name, approval_status="approved")
    agent.base_model_id = base.id
    db.commit()
    db.refresh(agent)
    return agent


async def _flush(queue) -> None:
    batch = []
    while not queue.empty():
        batch.append(queue.get_nowait())
    await usage_writer._flush_batch(batch)


@pytest.mark.asyncio
async def test_dispatch_jwt_bills_registered_model_to_the_asker(
    client, db: Session, usage_queue
):
    """派工 JWT 打註冊模型：200，用量在提問者身上，model 是底層模型。"""
    dept = _department(db)
    asker = make_user(db, username="asker-bill", role="user", department_id=dept.id)
    owner = make_user(db, username="owner-bill", role="developer")
    # 兩張表各自從 1 編號。先占一個 agent，避免 base.id 與 agent.id 撞號，
    # 否則「model_id 不是 agent id」這條斷言分不出記錯人。
    make_agent(db, owner, name="id-spacer", approval_status="registered")
    base = make_model(db, name="gemma-approved")
    other = make_model(db, name="gemma-other")
    agent = _approved_agent(db, owner=owner, name="hr-helper", base=base)
    assert base.id != agent.id
    assert (
        db.query(UserModelPermission)
        .filter(UserModelPermission.user_id == asker.id)
        .count()
        == 0
    )
    assert check_model_permission(
        db, user=asker, api_key_id=None, model_id=base.id
    ) is False

    token = issue_dispatch_token(
        user_id=asker.id, department=dept.id, agent_id=agent.id
    )
    resp = client.post(
        "/v1/chat/completions",
        headers=_bearer(token),
        json={
            "model": base.name,
            "stream": False,
            "messages": [{"role": "user", "content": "請假規定"}],
        },
    )
    assert resp.status_code == 200, resp.text
    assert _RecordingClient.posts, "應該把請求轉給底層模型"
    assert _RecordingClient.posts[0]["json"]["model"] == base.name

    await _flush(usage_queue)
    db.expire_all()
    rows = db.query(TokenUsage).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id == asker.id
    assert row.department_id == dept.id
    assert row.caller_agent_id == agent.id
    assert row.model_id == base.id
    assert row.model_id != agent.id
    assert row.model_id != other.id
    assert row.total_tokens == 18
    grouped = get_top_agents(db, days=1)
    assert grouped[0]["agent_id"] == agent.id
    assert grouped[0]["total_tokens"] == 18


@pytest.mark.asyncio
async def test_dispatch_jwt_stream_also_bills_the_asker(
    client, db: Session, usage_queue, monkeypatch
):
    """快速包打的是串流。同一枚派工 JWT 也要記到提問者與底層模型。"""
    monkeypatch.setattr(
        proxy_service.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _StreamClient(*args, **kwargs),
    )
    dept = _department(db, name="串流處")
    asker = make_user(db, username="asker-stream", role="user", department_id=dept.id)
    owner = make_user(db, username="owner-stream", role="developer")
    make_agent(db, owner, name="stream-spacer", approval_status="registered")
    base = make_model(db, name="stream-base")
    agent = _approved_agent(db, owner=owner, name="stream-helper", base=base)
    assert check_model_permission(
        db, user=asker, api_key_id=None, model_id=base.id
    ) is False
    token = issue_dispatch_token(
        user_id=asker.id, department=dept.id, agent_id=agent.id
    )
    resp = client.post(
        "/v1/chat/completions",
        headers=_bearer(token),
        json={
            "model": base.name,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200, resp.text
    assert "text/event-stream" in resp.headers["content-type"]
    _ = resp.text
    await _flush(usage_queue)
    db.expire_all()
    row = db.query(TokenUsage).one()
    assert row.user_id == asker.id
    assert row.department_id == dept.id
    assert row.caller_agent_id == agent.id
    assert row.model_id == base.id
    assert base.id != agent.id


def test_dispatch_jwt_rejects_any_model_but_the_registered_one(client, db, usage_queue):
    dept = _department(db, name="研發處")
    asker = make_user(db, username="asker-other", role="user", department_id=dept.id)
    owner = make_user(db, username="owner-other", role="developer")
    base = make_model(db, name="only-this-model")
    other = make_model(db, name="not-this-model")
    agent = _approved_agent(db, owner=owner, name="only-agent", base=base)
    token = issue_dispatch_token(
        user_id=asker.id, department=dept.id, agent_id=agent.id
    )
    resp = client.post(
        "/v1/chat/completions",
        headers=_bearer(token),
        json={
            "model": other.name,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == f"此 agent 只核准使用 {base.name}"
    assert usage_queue.empty()
    assert _RecordingClient.posts == []


def test_expired_dispatch_jwt_is_401(client, db):
    dept = _department(db, name="過期處")
    asker = make_user(db, username="asker-exp", role="user", department_id=dept.id)
    owner = make_user(db, username="owner-exp", role="developer")
    base = make_model(db, name="exp-model")
    agent = _approved_agent(db, owner=owner, name="exp-agent", base=base)
    past = datetime.now(timezone.utc) - timedelta(minutes=10)
    claims = build_dispatch_claims(
        user_id=asker.id,
        department=dept.id,
        agent_id=agent.id,
        issued_at=past - timedelta(minutes=5),
        expires_at=past,
    )
    resp = client.post(
        "/v1/chat/completions",
        headers=_bearer(_signed(claims)),
        json={"model": base.name, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 401, resp.text
    assert "派工" in resp.json()["detail"]


def test_unapproved_agent_dispatch_jwt_is_403(client, db):
    dept = _department(db, name="未核准處")
    asker = make_user(db, username="asker-unap", role="user", department_id=dept.id)
    owner = make_user(db, username="owner-unap", role="developer")
    base = make_model(db, name="unap-model")
    agent = make_agent(db, owner, name="unap-agent", approval_status="registered")
    agent.base_model_id = base.id
    db.commit()
    token = issue_dispatch_token(
        user_id=asker.id, department=dept.id, agent_id=agent.id
    )
    resp = client.post(
        "/v1/chat/completions",
        headers=_bearer(token),
        json={"model": base.name, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 403, resp.text
    assert "核准" in resp.json()["detail"]


def test_missing_agent_dispatch_jwt_is_401(client, db):
    dept = _department(db, name="缺席處")
    asker = make_user(db, username="asker-missing", role="user", department_id=dept.id)
    token = issue_dispatch_token(user_id=asker.id, department=dept.id, agent_id=987654)
    resp = client.post(
        "/v1/chat/completions",
        headers=_bearer(token),
        json={"model": "whatever", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 401, resp.text
    assert "agent" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_dispatch_hop_does_not_write_token_usage(
    db: Session, usage_queue, monkeypatch
):
    """派工到 agent 這一跳不打模型，不寫 token_usage。"""
    monkeypatch.setattr(proxy_api, "_schedule_memory_write", lambda **kwargs: None)
    caller = make_user(db, username="hop-caller", role="admin")
    agent = make_agent(db, caller, name="hop-agent", approval_status="approved")

    response = await proxy_api.chat_completions(
        _Request(
            {
                "model": agent.name,
                "stream": False,
                "messages": [{"role": "user", "content": "question"}],
            }
        ),
        caller=Caller(user=caller, api_key_id=None),
        db=db,
    )
    assert response["choices"][0]["message"]["content"] == "answer"
    assert usage_queue.empty()
    assert db.query(TokenUsage).count() == 0


@pytest.mark.asyncio
async def test_dispatch_hop_stream_does_not_write_token_usage(
    db: Session, usage_queue, monkeypatch
):
    monkeypatch.setattr(proxy_api, "_schedule_memory_write", lambda **kwargs: None)
    monkeypatch.setattr(
        proxy_service.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _StreamClient(*args, **kwargs),
    )
    caller = make_user(db, username="hop-stream", role="admin")
    agent = make_agent(db, caller, name="hop-stream-agent", approval_status="approved")
    response = await proxy_api.chat_completions(
        _Request(
            {
                "model": agent.name,
                "stream": True,
                "messages": [{"role": "user", "content": "question"}],
            }
        ),
        caller=Caller(user=caller, api_key_id=None),
        db=db,
    )
    async for _chunk in response.body_iterator:
        pass
    assert usage_queue.empty()
    assert db.query(TokenUsage).count() == 0


def test_deactivating_base_model_makes_agent_unavailable(client, db, usage_queue):
    owner = make_user(db, username="owner-off", role="developer")
    admin = make_user(db, username="admin-off", role="admin")
    user = make_user(db, username="user-off", role="user")
    base = make_model(db, name="going-offline")
    agent = _approved_agent(db, owner=owner, name="offline-helper", base=base)
    agent.health_status = "healthy"
    db.add(UserAgentPermission(user_id=user.id, agent_id=agent.id))
    db.commit()
    assert agent.approval_status == "approved"

    resp = client.delete(
        f"/api/models/{base.id}",
        headers=_bearer(login(client, "admin-off")),
    )
    assert resp.status_code == 200, resp.text
    db.expire_all()
    db.refresh(agent)
    assert agent.unavailable_reason
    assert agent.approval_status == "approved"
    assert agent.health_status == "healthy"

    notes = (
        db.query(Notification)
        .filter(Notification.user_id == owner.id)
        .all()
    )
    assert notes
    assert any("重新" in note.body and "模型" in note.body for note in notes)

    listed = client.get(
        "/v1/agents",
        headers=_bearer(create_tokens(admin)["access_token"]),
    )
    assert listed.status_code == 200, listed.text
    names = {row["id"] for row in listed.json()["data"]}
    assert agent.name not in names

    chat = client.post(
        "/v1/chat/completions",
        headers=_bearer(create_tokens(user)["access_token"]),
        json={
            "model": agent.name,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert chat.status_code == 403, chat.text
    assert chat.json()["detail"] == UNAVAILABLE


def test_purging_base_model_makes_agent_unavailable(client, db):
    owner = make_user(db, username="owner-purge", role="owner")
    base = make_model(db, name="going-away")
    agent = _approved_agent(db, owner=owner, name="purge-helper", base=base)
    resp = client.delete(
        f"/api/models/{base.id}/purge",
        headers=_bearer(login(client, "owner-purge")),
    )
    assert resp.status_code == 200, resp.text
    db.expire_all()
    db.refresh(agent)
    assert agent.unavailable_reason
    assert agent.base_model_id is None
    notes = db.query(Notification).filter(Notification.user_id == owner.id).all()
    assert notes
    assert any("模型" in note.body for note in notes)


def test_changing_base_model_resets_approval(client, db):
    owner = make_user(db, username="owner-reapprove", role="developer")
    admin = make_user(db, username="admin-reapprove", role="admin")
    first = make_model(db, name="model-a")
    second = make_model(db, name="model-b")
    agent = _approved_agent(db, owner=owner, name="switch-helper", base=first)
    agent.approved_by = admin.id
    agent.approved_at = datetime.now(timezone.utc)
    db.commit()

    resp = client.put(
        f"/api/agents/{agent.id}",
        headers=_bearer(login(client, "owner-reapprove")),
        json={"base_model_id": second.id},
    )
    assert resp.status_code == 200, resp.text
    db.expire_all()
    db.refresh(agent)
    assert agent.base_model_id == second.id
    assert agent.approval_status == "registered"
    assert agent.approved_by is None
    assert agent.approved_at is None
