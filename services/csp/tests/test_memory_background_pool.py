"""背景記憶整理在單一實體連線下仍須讓請求歸還連線。"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, get_ident

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from app.services import memory_service
from app.database import Base
from app.models.model_registry import ModelRegistry
from app.models.platform_setting import set_setting
from app.models.user_memory import ConversationSummary, MemoryRefreshLease, UserFact
from tests.conftest import make_user
from tests.test_memory_review_fixes import _conv, _pair, _summary_role


@dataclass
class PoolMemory:
    engine: object
    factory: object
    conversation_id: int
    user_id: int
    sessions: list
    proxy_calls: list


@pytest.fixture
def pool_memory(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'memory-pool.sqlite'}",
        connect_args={"check_same_thread": False},
        poolclass=QueuePool, pool_size=1, max_overflow=0, pool_timeout=0.25,
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    Base.metadata.create_all(engine)
    with factory() as db:
        user = make_user(db, username="synthetic-memory-pool")
        conv = _conv(db, user)
        _pair(
            db, conv,
            "我負責行政採購，專案採用離線文件整理，每月彙整採購清冊並保留原始單據供核對。",
            "已收到清冊與單據整理需求。",
            when=datetime.now(timezone.utc) - timedelta(hours=3),
        )
        _summary_role(db, "synthetic-summary")
        db.add(ModelRegistry(
            name="synthetic-embedding", display_name="合成向量",
            model_type="embedding", endpoint_url="http://synthetic.invalid/v1",
            is_active=True, is_platform_embedding=True, embedding_native_dim=2,
        ))
        set_setting(db, "memory.enabled", True)
        db.commit()
        conversation_id, user_id = conv.id, user.id
    sessions, proxy_calls = [], []
    main_thread = get_ident()

    class WorkerSession(Session):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.owner = get_ident()
            self.closed_by_owner = False
            assert self.owner != main_thread
            sessions.append(self)
            event.listen(self, "after_begin", self.check_owner)

        def check_owner(self, *_args):
            assert get_ident() == self.owner

        def close(self):
            assert get_ident() == self.owner
            super().close()
            self.closed_by_owner = True

    worker_factory = sessionmaker(
        bind=engine, class_=WorkerSession, expire_on_commit=False, autoflush=False,
    )
    monkeypatch.setattr(memory_service, "SessionLocal", worker_factory)

    async def proxy_request(**kwargs):
        assert get_ident() == main_thread
        assert engine.pool.checkedout() == 0
        assert not isinstance(kwargs["model"], ModelRegistry)
        assert kwargs["user_id"] == user_id
        proxy_calls.append(kwargs)
        if kwargs["endpoint_path"].endswith("/embeddings"):
            assert kwargs["record_usage"] is False
            return {"data": [{"index": 0, "embedding": [1.0, 0.0]}]}
        assert kwargs["usage_kind"] == "platform"
        assert kwargs["request_type_override"] == "internal"
        return {"choices": [{"message": {"content": json.dumps({
            "summary": "使用者負責行政採購，專案採用離線文件整理，每月彙整清冊並保留單據。",
            "facts": [{"key": "專案技術選擇", "value": "離線文件整理", "confidence": 0.9}],
        }, ensure_ascii=False)}}]}

    monkeypatch.setattr("app.services.proxy.service.proxy_request", proxy_request)
    result = PoolMemory(engine, factory, conversation_id, user_id, sessions, proxy_calls)
    yield result
    assert engine.pool.checkedout() == 0
    assert all(session.closed_by_owner for session in sessions)
    engine.dispose()


@pytest.mark.asyncio
async def test_nonempty_due_ids_allow_scheduled_connection_release(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'nonempty-pool.sqlite'}",
        connect_args={"check_same_thread": False},
        poolclass=QueuePool, pool_size=1, max_overflow=0, pool_timeout=0.25,
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    queried = []

    def enabled(db):
        assert db.execute(text("SELECT 1")).scalar_one() == 1
        queried.append(True)
        return False

    monkeypatch.setattr(memory_service, "SessionLocal", factory)
    monkeypatch.setattr(memory_service, "_other_conversations_due", lambda *_args: [1])
    monkeypatch.setattr(memory_service, "_memory_enabled", enabled)
    held = factory()
    held.execute(text("SELECT 1"))
    task = asyncio.create_task(memory_service.flush_other_conversations(1, 2))
    try:
        await asyncio.sleep(0.025)
        assert not task.done()
        held.close()
        await asyncio.wait_for(task, timeout=1)
        assert queried == [True]
        assert engine.pool.checkedout() == 0
    finally:
        held.close()
        await asyncio.wait_for(task, timeout=1)
        engine.dispose()


def _assert_saved(pool):
    with pool.factory() as db:
        summary = db.query(ConversationSummary).one()
        assert summary.conversation_id == pool.conversation_id
        assert summary.covered_message_id is not None
        assert summary.embedding_source_model == "synthetic-embedding"
        fact = db.query(UserFact).one()
        assert fact.value == "離線文件整理"
        assert fact.source_conversation_id == pool.conversation_id
        assert fact.embedding_source_model == "synthetic-embedding"
        assert db.get(MemoryRefreshLease, pool.conversation_id).claimed_until == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["direct", "other", "idle"])
async def test_real_nonempty_refresh_completes_with_single_connection(pool_memory, entry):
    pool = pool_memory
    held = pool.factory()
    held.execute(text("SELECT 1"))
    if entry == "direct":
        coro = memory_service.refresh_conversation(pool.conversation_id)
    elif entry == "other":
        coro = memory_service.flush_other_conversations(pool.user_id, -1)
    else:
        coro = memory_service.flush_idle_conversations()
    task = asyncio.create_task(coro)
    try:
        await asyncio.sleep(0.025)
        assert not task.done()
        held.close()
        await asyncio.wait_for(task, timeout=2)
        assert len(pool.proxy_calls) == 3
        _assert_saved(pool)
    finally:
        held.close()
        await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("after_call", [1, 3])
async def test_recheckout_after_model_io_keeps_loop_available(pool_memory, monkeypatch, after_call):
    pool = pool_memory
    from app.services.proxy import service as proxy_service

    original = proxy_service.proxy_request
    releases = []
    held = pool.factory()

    async def contend(**kwargs):
        result = await original(**kwargs)
        if len(pool.proxy_calls) == after_call:
            held.execute(text("SELECT 1"))

            def release():
                held.close()
                releases.append(True)

            asyncio.get_running_loop().call_later(0.025, release)
        return result

    monkeypatch.setattr(proxy_service, "proxy_request", contend)
    try:
        await asyncio.wait_for(memory_service.refresh_conversation(pool.conversation_id), 2)
        assert releases == [True]
        _assert_saved(pool)
    finally:
        held.close()


@pytest.mark.asyncio
async def test_cancelled_checkout_is_reaped_without_late_writes(pool_memory):
    pool = pool_memory
    held = pool.factory()
    held.execute(text("SELECT 1"))
    task = asyncio.create_task(memory_service.refresh_conversation(pool.conversation_id))
    try:
        await asyncio.sleep(0.025)
        task.cancel()
        await asyncio.sleep(0.025)
        assert not task.done(), "取消必須等擁有 Session 的工作執行緒關閉它"
        held.close()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert all(session.closed_by_owner for session in pool.sessions)
        assert pool.proxy_calls == []
        with pool.factory() as db:
            assert db.query(ConversationSummary).count() == 0
            assert db.query(UserFact).count() == 0
            assert db.query(MemoryRefreshLease).count() == 0
    finally:
        held.close()
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_model_call_defers_lease_and_closes_sessions(pool_memory, monkeypatch):
    pool = pool_memory
    entered = asyncio.Event()

    async def pending(**_kwargs):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr("app.services.proxy.service.proxy_request", pending)
    task = asyncio.create_task(memory_service.refresh_conversation(pool.conversation_id))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    with pool.factory() as db:
        assert db.query(ConversationSummary).count() == 0
        assert db.query(UserFact).count() == 0
        assert db.get(MemoryRefreshLease, pool.conversation_id).claimed_until > memory_service._now_epoch()


@pytest.mark.asyncio
async def test_cancelled_preparation_cleans_lease_before_plan_is_delivered(pool_memory, monkeypatch):
    pool = pool_memory
    entered = asyncio.Event()
    release = Event()
    loop = asyncio.get_running_loop()
    original = memory_service._prepare_refresh

    def pause(*args, **kwargs):
        plan = original(*args, **kwargs)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(1)
        return plan

    monkeypatch.setattr(memory_service, "_prepare_refresh", pause)
    task = asyncio.create_task(memory_service.refresh_conversation(pool.conversation_id))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert pool.proxy_calls == []
        with pool.factory() as db:
            assert db.query(ConversationSummary).count() == 0
            assert db.query(UserFact).count() == 0
            assert db.get(MemoryRefreshLease, pool.conversation_id).claimed_until > memory_service._now_epoch()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_fact_phase_rewinds_coverage_before_return(pool_memory, monkeypatch):
    pool = pool_memory
    entered = asyncio.Event()
    release = Event()
    loop = asyncio.get_running_loop()
    original = memory_service._upsert_facts_generic

    def pause(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(memory_service, "_upsert_facts_generic", pause)
    task = asyncio.create_task(memory_service.refresh_conversation(pool.conversation_id))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0.025)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        with pool.factory() as db:
            assert db.query(ConversationSummary).one().covered_message_id is None
            assert db.query(UserFact).count() == 0
            assert db.get(MemoryRefreshLease, pool.conversation_id).claimed_until > memory_service._now_epoch()
        await asyncio.sleep(0)
        assert all(session.closed_by_owner for session in pool.sessions)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["model", "facts"])
async def test_background_failures_defer_lease_and_return_connection(pool_memory, monkeypatch, failure):
    pool = pool_memory

    def fail(*_args, **_kwargs):
        raise ValueError("synthetic failure")

    if failure == "model":
        async def failed_model(**_kwargs):
            from fastapi import HTTPException
            raise HTTPException(503, "合成模型暫時無法使用")
        monkeypatch.setattr("app.services.proxy.service.proxy_request", failed_model)
    else:
        monkeypatch.setattr(memory_service, "_upsert_facts_generic", fail)
    await asyncio.wait_for(memory_service.refresh_conversation(pool.conversation_id), 2)
    with pool.factory() as db:
        assert db.query(UserFact).count() == 0
        if failure == "facts":
            assert db.query(ConversationSummary).one().covered_message_id is None
        else:
            assert db.query(ConversationSummary).count() == 0
        assert db.get(MemoryRefreshLease, pool.conversation_id).claimed_until > memory_service._now_epoch()


@pytest.mark.asyncio
async def test_background_refresh_limits_workers_before_checkout(pool_memory, monkeypatch):
    pool = pool_memory
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = Event()
    lock = Lock()
    active = 0
    peak = 0

    def enabled(db):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 4:
                loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(1)
            assert db.execute(text("SELECT 1")).scalar_one() == 1
            return False
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(memory_service, "_memory_enabled", enabled)
    tasks = [asyncio.create_task(memory_service.refresh_conversation(999)) for _ in range(9)]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert len(pool.sessions) == 4
        assert pool.engine.pool.checkedout() == 0
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert peak == 4
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["committed", "cleanup"])
async def test_late_cancellation_keeps_completed_data_and_propagates(pool_memory, monkeypatch, phase):
    pool = pool_memory
    entered = asyncio.Event()
    release = Event()
    loop = asyncio.get_running_loop()
    name = "_persist_refresh" if phase == "committed" else "_release_refresh"
    original = getattr(memory_service, name)

    def pause(*args, **kwargs):
        if phase == "committed":
            original(*args, **kwargs)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(1)
        if phase == "cleanup":
            original(*args, **kwargs)

    monkeypatch.setattr(memory_service, name, pause)
    task = asyncio.create_task(memory_service.refresh_conversation(pool.conversation_id))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        _assert_saved(pool)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_failure_does_not_rewind_a_new_lease_owner(pool_memory, monkeypatch):
    pool = pool_memory

    def replaced_owner(db, *_args, **_kwargs):
        db.execute(text(
            "UPDATE memory_refresh_leases SET claim_token='synthetic-new-owner', "
            "claimed_until=:until WHERE conversation_id=:cid"
        ), {"until": memory_service._now_epoch() + 600, "cid": pool.conversation_id})
        db.execute(text(
            "UPDATE conversation_summaries SET covered_message_id=covered_message_id+2 "
            "WHERE conversation_id=:cid"
        ), {"cid": pool.conversation_id})
        db.commit()
        raise ValueError("synthetic newer owner")

    monkeypatch.setattr(memory_service, "_upsert_facts_generic", replaced_owner)
    await asyncio.wait_for(memory_service.refresh_conversation(pool.conversation_id), 2)
    with pool.factory() as db:
        summary = db.query(ConversationSummary).one()
        assert summary.covered_message_id == 4
        lease = db.get(MemoryRefreshLease, pool.conversation_id)
        assert lease.claim_token == "synthetic-new-owner"
        assert lease.claimed_until > memory_service._now_epoch()
