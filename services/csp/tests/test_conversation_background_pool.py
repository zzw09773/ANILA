"""背景摘要開始前須釋放請求連線，避免小型連線池互等。"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import BackgroundTasks, Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from app.api.conversations import (
    ConversationCreate, MessageUpdate, TurnHeadCreate, create_conversation,
    get_conversation, start_turn, update_message,
)
from app.database import Base
from app.models.router_model_grant import RouterModelGrant
from app.services import memory_service
from app.services import auth_service
from app.services import conversation_service
from tests.conftest import make_model, make_user


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_model", [False, True])
async def test_create_releases_serialization_transaction_before_background(
    tmp_path, monkeypatch, explicit_model,
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'background-pool.sqlite'}",
        connect_args={"check_same_thread": False},
        poolclass=QueuePool, pool_size=1, max_overflow=0, pool_timeout=0.1,
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    background_checkouts = []
    background_active = False

    def checkout(*_args):
        if background_active:
            background_checkouts.append(True)

    event.listen(engine, "checkout", checkout)
    original_flush = memory_service.flush_other_conversations

    async def flush(user_id, exclude_id):
        nonlocal background_active
        background_active = True
        await original_flush(user_id, exclude_id)

    monkeypatch.setattr(memory_service, "SessionLocal", factory)
    monkeypatch.setattr(memory_service, "flush_other_conversations", flush)
    try:
        with factory() as db:
            user = make_user(db, username="background-pool-user")
            model_id = None
            if explicit_model:
                model = make_model(db, name="background-pool-model")
                model.router_enabled = True
                db.add(RouterModelGrant(scope_type="user", user_id=user.id, model_id=model.id))
                model_id = model.id
                db.commit()
            tasks = BackgroundTasks()
            result = create_conversation(
                ConversationCreate(title="合成對話", origin="anila-ui", router_model_id=model_id),
                tasks, db, user,
            )
            # 保持請求 Session 活著，重現依賴清理晚於背景工作的生命週期。
            await tasks()
            assert background_checkouts, "背景查詢未能取得唯一連線"
            assert engine.pool.checkedout() == 0
            assert result.id > 0 and result.title == "合成對話"
            assert result.router_model_id == model_id
    finally:
        engine.dispose()


@pytest.mark.parametrize("operation", ["turn", "finalize", "detail"])
def test_chat_crud_response_returns_connection_before_next_request(
    tmp_path, operation,
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'crud-pool.sqlite'}",
        connect_args={"check_same_thread": False},
        poolclass=QueuePool, pool_size=1, max_overflow=0, pool_timeout=0.1,
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    try:
        with factory() as db:
            user = make_user(db, username="crud-pool-user")
            conv = conversation_service.create_conversation(db, user.id, title="合成對話")
            conv_id = conv.id
            db.commit()
            head = start_turn(
                conv_id, TurnHeadCreate(content="合成問題", stream_writer="synthetic-writer"),
                db, user,
            )
            if operation == "turn":
                assert head.user.content == "合成問題" and head.assistant.content == ""
            elif operation == "finalize":
                finalized = update_message(
                    conv_id, head.assistant.id,
                    MessageUpdate(content="合成回答", stream_writer="synthetic-writer",
                                  metadata={"anila_stream": {"state": "complete"}}),
                    db, user,
                )
                assert finalized.content == "合成回答"
            else:
                detail = get_conversation(conv_id, "active", db, user)
                assert [message.content for message in detail.messages] == ["合成問題", ""]
            with factory() as next_request:
                assert next_request.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        engine.dispose()


def test_authenticated_identity_releases_pool_before_endpoint_dispatch(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'identity-pool.sqlite'}",
        connect_args={"check_same_thread": False},
        poolclass=QueuePool, pool_size=1, max_overflow=0, pool_timeout=0.1,
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    try:
        with factory() as db:
            user = make_user(db, username="identity-pool-user")
            user_id = user.id
            db.commit()
            db.expunge_all()

            def resolved(token, session):
                assert token == "synthetic-presented-token"
                authenticated = session.get(type(user), user_id)
                assert engine.pool.checkedout() == 1
                return authenticated

            monkeypatch.setattr(auth_service, "resolve_presented_user", resolved)
            result = auth_service.get_current_user(
                Request({"type": "http", "headers": []}),
                HTTPAuthorizationCredentials(scheme="Bearer", credentials="synthetic-presented-token"),
                db,
            )
            # 身分欄位可在派發端點前讀取，另一請求也能取得唯一連線。
            assert result.id == user_id and result.username == "identity-pool-user"
            with factory() as next_request:
                assert next_request.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_background_pool_wait_keeps_event_loop_available(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'waiting-pool.sqlite'}",
        connect_args={"check_same_thread": False},
        poolclass=QueuePool, pool_size=1, max_overflow=0, pool_timeout=0.5,
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    queried = []

    def due(db, **_kwargs):
        assert db.execute(text("SELECT 1")).scalar_one() == 1
        queried.append(True)
        return []

    monkeypatch.setattr(memory_service, "SessionLocal", factory)
    monkeypatch.setattr(memory_service, "conversations_due", due)
    held = factory()
    held.execute(text("SELECT 1"))
    task = asyncio.create_task(memory_service.flush_other_conversations(1, 2))
    try:
        await asyncio.sleep(0.03)
        # 連線仍被另一請求持有時，背景查詢應等待，事件迴圈仍能釋放它。
        assert not task.done()
        held.close()
        await asyncio.wait_for(task, timeout=1)
        assert queried == [True]
        assert engine.pool.checkedout() == 0
    finally:
        held.close()
        await asyncio.wait_for(task, timeout=1)
        engine.dispose()
