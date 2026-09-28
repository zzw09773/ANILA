"""Chat proxy must not pin a pooled connection across the upstream model.

Auth, grants, memory, KB and attachments run first. The request session is
closed before the fair-queue wait and before the upstream call, including
while a streaming body is still open. Usage and audit open a later session.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from app.database import Base, get_db
from app.main import app
from app.models.model_registry import ModelRegistry
from app.models.user import User
from app.services.auth_service import create_tokens
from app.services.model_gate import ModelAdmission, reset_board_for_tests
from app.services.proxy.dispatch_chat import DispatchModelCall
from app.utils.security import hash_password
from tests.conftest import make_agent


def _engine():
    # Shared-cache memory: QueuePool hands out distinct connections, and a
    # plain sqlite:// memory database would be empty on the second one.
    import app.models.jwt_signing_key  # noqa: F401
    import app.models.feedback_read  # noqa: F401

    engine = create_engine(
        "sqlite:///file:chat-pool-test?mode=memory&cache=shared&uri=true",
        connect_args={"check_same_thread": False},
        poolclass=QueuePool,
        pool_size=2,
        max_overflow=0,
    )
    Base.metadata.create_all(bind=engine)
    return engine


def _listen(engine):
    @event.listens_for(engine, "checkout")
    def _on_checkout(dbapi_conn, connection_record, connection_proxy):  # noqa: ARG001
        return None

    @event.listens_for(engine, "checkin")
    def _on_checkin(dbapi_conn, connection_record):  # noqa: ARG001
        return None


class _StreamResponse:
    def __init__(self, lines, held, engine):
        self.status_code = 200
        self._lines = lines
        self._held = held
        self._engine = engine

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_lines(self):
        self._held.append(self._engine.pool.checkedout())
        await asyncio.sleep(0.05)
        self._held.append(self._engine.pool.checkedout())
        for line in self._lines:
            yield line
        self._held.append(self._engine.pool.checkedout())

    async def aread(self):
        return b""


class _PostResponse:
    status_code = 200
    text = ""
    headers = {"content-type": "application/json"}

    def json(self):
        return {
            "choices": [{"message": {"role": "assistant", "content": "hello"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }


class _FakeClient:
    def __init__(self, held, engine, lines, *args, **kwargs):
        self._held = held
        self._engine = engine
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method, url, json=None, headers=None):
        return _StreamResponse(self._lines, self._held, self._engine)

    async def post(self, url, json=None, headers=None):
        self._held.append(self._engine.pool.checkedout())
        await asyncio.sleep(0.05)
        self._held.append(self._engine.pool.checkedout())
        return _PostResponse()


@pytest.fixture
def pool_client(monkeypatch):
    engine = _engine()
    _listen(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    reset_board_for_tests()
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")

    def override_get_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    held: list[int] = []
    lines = [
        'data: {"choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":null}]}',
        "",
        "data: [DONE]",
        "",
    ]

    def factory(*args, **kwargs):
        return _FakeClient(held, engine, lines, *args, **kwargs)

    monkeypatch.setattr("app.services.proxy.service.httpx.AsyncClient", factory)

    seen: list[str] = []

    import app.api.proxy as proxy_api

    real_stream = proxy_api.proxy_stream

    async def wrapped_stream(**kwargs):
        model = kwargs.get("model")
        seen.append(type(model).__name__ if model is not None else "None")
        async for chunk in real_stream(**kwargs):
            yield chunk

    monkeypatch.setattr(proxy_api, "proxy_stream", wrapped_stream)

    real_request = proxy_api.proxy_request

    async def wrapped_request(**kwargs):
        model = kwargs.get("model")
        seen.append(type(model).__name__)
        return await real_request(**kwargs)

    monkeypatch.setattr(proxy_api, "proxy_request", wrapped_request)

    db = Session()
    try:
        user = User(
            username="pool-chat",
            hashed_password=hash_password("password"),
            role="admin",
            is_active=True,
            is_approved=True,
        )
        model = ModelRegistry(
            name="pool-llm",
            display_name="pool-llm",
            model_type="llm",
            endpoint_url="http://mock-llm/v1",
            api_version="v1",
            is_active=True,
            max_concurrent=1,
        )
        db.add_all([user, model])
        db.commit()
        db.refresh(user)
        db.refresh(model)
        db.commit()
        token = create_tokens(user)["access_token"]
        db.close()
        with TestClient(app, raise_server_exceptions=True) as client:
            yield SimpleNamespace(
                client=client,
                engine=engine,
                held=held,
                seen=seen,
                token=token,
                user=user,
                model=model,
                Session=Session,
            )
    finally:
        db.close()
        app.dependency_overrides.clear()
        reset_board_for_tests()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_stream_holds_no_connection_while_upstream_or_queued(pool_client, monkeypatch):
    during_body: list[int] = []
    during_queue: list[int] = []
    engine = pool_client.engine
    real_json = Request.json

    async def slow_json(self):
        during_body.append(engine.pool.checkedout())
        await asyncio.sleep(0.02)
        during_body.append(engine.pool.checkedout())
        return await real_json(self)

    monkeypatch.setattr(Request, "json", slow_json)

    async def watching_wait(self):
        during_queue.append(engine.pool.checkedout())
        await asyncio.sleep(0.05)
        during_queue.append(engine.pool.checkedout())
        self.held = True
        return
        yield 0

    monkeypatch.setattr(ModelAdmission, "wait_positions", watching_wait)

    with pool_client.client.stream(
        "POST",
        "/v1/chat/completions",
        headers=_auth(pool_client.token),
        json={
            "model": "pool-llm",
            "messages": [{"role": "user", "content": "你好"}],
            "stream": True,
        },
    ) as response:
        assert response.status_code == 200, response.read()
        body = response.read().decode()

    assert "hello" in body
    assert during_body, "body read was not observed"
    assert during_queue, "queue wait was not observed"
    assert pool_client.held, "fake upstream stream did not run"
    assert all(n == 0 for n in during_body), during_body
    assert all(n == 0 for n in during_queue), during_queue
    assert all(n == 0 for n in pool_client.held), pool_client.held
    assert pool_client.seen == ["SimpleNamespace"]


def test_nonstream_holds_no_connection_during_upstream(pool_client):
    response = pool_client.client.post(
        "/v1/chat/completions",
        headers=_auth(pool_client.token),
        json={
            "model": "pool-llm",
            "messages": [{"role": "user", "content": "你好"}],
            "stream": False,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "hello"
    assert pool_client.held, "fake upstream post did not run"
    assert all(n == 0 for n in pool_client.held), pool_client.held
    assert pool_client.seen == ["SimpleNamespace"]


def test_dispatched_model_call_holds_no_connection_during_upstream(pool_client, monkeypatch):
    owner = User(
        username="pool-owner",
        hashed_password=hash_password("password"),
        role="developer",
        is_active=True,
        is_approved=True,
    )
    db = pool_client.Session()
    try:
        db.add(owner)
        db.commit()
        db.refresh(owner)
        agent = make_agent(db, owner, name="pool-agent", approval_status="approved")
        agent.base_model_id = pool_client.model.id
        db.commit()
        db.refresh(agent)
        _ = agent.base_model
        db.commit()
        call = DispatchModelCall(
            user=pool_client.user,
            agent=agent,
            department_id=None,
        )
        db.close()
    finally:
        db.close()

    def override_caller():
        return call

    from app.services.proxy.dispatch_chat import resolve_chat_caller

    app.dependency_overrides[resolve_chat_caller] = override_caller
    try:
        response = pool_client.client.post(
            "/v1/chat/completions",
            headers=_auth(pool_client.token),
            json={
                "model": "pool-llm",
                "messages": [{"role": "user", "content": "你好"}],
                "stream": False,
            },
        )
    finally:
        app.dependency_overrides.pop(resolve_chat_caller, None)

    assert response.status_code == 200, response.text
    assert pool_client.held, "dispatched upstream post did not run"
    assert all(n == 0 for n in pool_client.held), pool_client.held
    assert "SimpleNamespace" in pool_client.seen
