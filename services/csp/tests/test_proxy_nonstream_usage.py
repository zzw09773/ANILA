"""非串流 agent forward 的用量歸戶測試。"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.api import proxy as proxy_api
from app.middleware.caller import Caller
from app.models.token_usage import TokenUsage
from app.services import proxy_service, usage_writer
from tests.conftest import make_agent, make_user


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


class _AgentClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        return _PostResponse()


class _Request:
    def __init__(self, body: dict):
        self._body = body
        self.headers = {}

    async def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _isolated_agent_upstream(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "agent")
    monkeypatch.setattr(
        proxy_service.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _AgentClient(*args, **kwargs),
    )
    monkeypatch.setattr(proxy_service, "build_agent_headers", lambda **kwargs: {})
    monkeypatch.setattr(proxy_api, "_schedule_memory_write", lambda **kwargs: None)


@pytest.mark.asyncio
async def test_nonstreaming_agent_forward_writes_attributed_usage_row(
    db: Session, db_engine, monkeypatch
):
    """非串流 agent 回覆應寫入帶 caller、agent 與 token 數的用量列。"""
    caller = make_user(db, username="nonstream-usage-caller", role="admin")
    agent = make_agent(
        db,
        caller,
        name="nonstream-usage-agent",
        approval_status="approved",
    )
    monkeypatch.setattr(
        usage_writer,
        "SessionLocal",
        sessionmaker(bind=db_engine, expire_on_commit=False),
    )
    monkeypatch.setattr(usage_writer, "_usage_queue", None)

    response = await proxy_api.chat_completions(
        _Request(
            {
                "model": agent.name,
                "stream": False,
                "messages": [{"role": "user", "content": "agent question"}],
            }
        ),
        caller=Caller(user=caller, api_key_id=None),
        db=db,
    )

    assert response["usage"] == {
        "prompt_tokens": 7,
        "completion_tokens": 11,
        "total_tokens": 18,
    }
    queue = usage_writer.get_usage_queue()
    batch = [queue.get_nowait()]
    await usage_writer._flush_batch(batch)

    db.expire_all()
    row = (
        db.query(TokenUsage)
        .filter(TokenUsage.user_id == caller.id)
        .one()
    )
    assert row.user_id == caller.id
    assert row.caller_agent_id == agent.id
    assert row.model_id == agent.id
    assert row.prompt_tokens == 7
    assert row.completion_tokens == 11
    assert row.total_tokens == 18
