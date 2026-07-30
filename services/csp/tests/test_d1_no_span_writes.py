# -*- coding: utf-8 -*-
"""D1 acceptance — message path writes zero span rows; usage still lands.

Proves by counting (inspector + usage enqueue), not by reading the removed
emit helper. Removed endpoints must 404.
"""

from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.models.task import Task
from app.services import proxy_service
from app.services.auth_service import create_tokens
from app.services.proxy import service as proxy_impl
from app.services.proxy import task_link
from tests.conftest import make_model, make_user


@pytest.fixture(autouse=True)
def _dev_ssrf_allowances(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm,agent")


@pytest.fixture
def task_sessions(monkeypatch, db_engine):
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    monkeypatch.setattr(task_link, "SessionLocal", factory)
    return factory


@pytest.fixture
def captured_usage(monkeypatch):
    recorded: list[dict] = []

    async def _fake(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(proxy_impl, "enqueue_usage_task_linked", _fake)
    return recorded


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _jwt(user) -> str:
    return create_tokens(user)["access_token"]


class _PostResponse:
    def __init__(self, payload, status_code: int = 200):
        import json as _json

        self.status_code = status_code
        self.headers = {"content-type": "application/json"}
        self._payload = payload
        self.text = _json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _PostClient:
    last_headers: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).last_headers = dict(headers or {})
        return _PostResponse(
            {
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 4,
                    "total_tokens": 7,
                },
            }
        )


def test_trace_spans_table_absent_from_metadata():
    assert "trace_spans" not in Base.metadata.tables


def test_removed_endpoints_404(client: TestClient, db: Session):
    admin = make_user(db, username="d1_404", role="admin")
    token = _jwt(admin)
    headers = _bearer(token)

    assert client.get("/api/traces/any", headers=headers).status_code == 404
    assert (
        client.post(
            "/v1/traces/any/spans",
            headers=headers,
            json={
                "spans": [
                    {
                        "span_id": "sp1",
                        "span_type": "agent.run.finished",
                        "name": "x",
                    }
                ]
            },
        ).status_code
        == 404
    )
    # Agent must exist for path to reach a missing route vs agent 404.
    # Direct path on agents router: trace-test was removed → 404/405.
    from tests.conftest import make_agent

    agent = make_agent(db, admin, name="d1-agent")
    resp = client.post(
        f"/api/agents/{agent.id}/trace-test", headers=headers
    )
    assert resp.status_code == 404


def test_message_roundtrip_writes_zero_spans_and_keeps_usage(
    client: TestClient,
    db: Session,
    db_engine,
    monkeypatch,
    task_sessions,
    captured_usage,
):
    """Before/after: span table absent (0); usage enqueue still fires once."""
    insp = inspect(db_engine)
    before_has_table = insp.has_table("trace_spans")
    before_count = 0
    if before_has_table:
        before_count = db.execute(
            __import__("sqlalchemy").text("SELECT COUNT(*) FROM trace_spans")
        ).scalar()

    admin = make_user(db, username="d1_msg", role="admin")
    make_model(db, name="d1-llm")
    task = Task(
        title="d1",
        task_type="query",
        requester_user_id=admin.id,
        status="submitted",
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    monkeypatch.setattr(
        proxy_service.httpx,
        "AsyncClient",
        lambda *a, **k: _PostClient(*a, **k),
    )

    resp = client.post(
        "/v1/chat/completions",
        headers={**_bearer(_jwt(admin)), "X-ANILA-Task-Id": str(task.id)},
        json={
            "model": "d1-llm",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200, resp.text

    insp2 = inspect(db_engine)
    after_has_table = insp2.has_table("trace_spans")
    after_count = 0
    if after_has_table:
        after_count = db.execute(
            __import__("sqlalchemy").text("SELECT COUNT(*) FROM trace_spans")
        ).scalar()

    assert before_has_table is False
    assert after_has_table is False
    assert before_count == 0
    assert after_count == 0

    assert len(captured_usage) == 1
    assert captured_usage[0]["task_id"] == task.id
    assert captured_usage[0]["trace_id"] == task.trace_id
    assert captured_usage[0]["prompt_tokens"] == 3
    assert captured_usage[0]["completion_tokens"] == 4
    assert captured_usage[0]["total_tokens"] == 7
