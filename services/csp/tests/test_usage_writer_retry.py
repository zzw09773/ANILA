"""Each worker flushes its own queue, and a failed commit keeps the rows."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from app.services import usage_writer as uw


@pytest.fixture(autouse=True)
def _reset_usage_shutdown_state():
    uw._shutdown_deadline = None
    uw._shutdown_logged = False
    uw._remember_uncommitted(0)
    yield
    uw._shutdown_deadline = None
    uw._shutdown_logged = False
    uw._remember_uncommitted(0)


class _Down:
    def bulk_insert_mappings(self, model, batch):
        raise RuntimeError("db down")

    def commit(self):
        raise AssertionError("commit")

    def rollback(self):
        return None

    def close(self):
        return None


class _Ok:
    def __init__(self) -> None:
        self.committed = False

    def bulk_insert_mappings(self, model, batch):
        self.batch = list(batch)

    def commit(self):
        self.committed = True

    def rollback(self):
        return None

    def close(self):
        return None


def test_failed_commit_returns_the_same_rows(monkeypatch):
    monkeypatch.setattr(uw, "SessionLocal", lambda: _Down())
    row = {"user_id": 1, "model_id": 2, "total_tokens": 3, "outcome": "partial"}
    left = asyncio.run(uw._flush_batch([row]))
    assert left == [row]


def test_successful_commit_returns_nothing(monkeypatch):
    db = _Ok()
    monkeypatch.setattr(uw, "SessionLocal", lambda: db)
    left = asyncio.run(uw._flush_batch([{"user_id": 1}]))
    assert left == []
    assert db.committed is True


def test_usage_writer_starts_outside_the_leader_loops():
    text = Path(__file__).resolve().parents[1].joinpath("app/main.py").read_text(
        encoding="utf-8"
    )
    start = text.split("async def _start_singleton_loops", 1)[1]
    body = start.split("return [task for task in tasks if task is not None]", 1)[0]
    assert "start_usage_writer" not in body
    assert "usage_writer_task = await start_usage_writer()" in text
    assert "await stop_usage_writer(usage_writer_task)" in text


def test_rows_enqueued_right_before_shutdown_are_persisted(monkeypatch, db_engine):
    """A row that lands in the queue during the retry pause must be committed on shutdown."""
    from sqlalchemy.orm import sessionmaker

    from app.models.token_usage import TokenUsage

    state = {"opens": 0}
    real = sessionmaker(bind=db_engine, expire_on_commit=False)

    def session_local():
        state["opens"] += 1
        if state["opens"] == 1:
            return _Down()
        return real()

    monkeypatch.setattr(uw, "SessionLocal", session_local)
    monkeypatch.setattr(uw, "_usage_queue", asyncio.Queue())
    real_sleep = asyncio.sleep

    async def retry_sleep(delay):
        if delay >= 1:
            await uw.enqueue_usage(
                api_key_id=None,
                user_id=1,
                department_id=None,
                model_id=1,
                prompt_tokens=2,
                completion_tokens=3,
                total_tokens=5,
                invocation_id="just-before-shutdown",
            )
            raise asyncio.CancelledError()
        await real_sleep(delay)

    monkeypatch.setattr(uw.asyncio, "sleep", retry_sleep)

    async def scenario():
        await uw.enqueue_usage(
            api_key_id=None,
            user_id=1,
            department_id=None,
            model_id=1,
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            invocation_id="already-in-batch",
        )
        task = asyncio.create_task(uw._usage_writer_loop())
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    session = real()
    try:
        stored = {row.invocation_id for row in session.query(TokenUsage).all()}
    finally:
        session.close()
    assert "just-before-shutdown" in stored
    assert "already-in-batch" in stored


def test_flush_past_the_drain_deadline_does_not_open_a_session(monkeypatch):
    def explode():
        raise AssertionError("session opened")

    monkeypatch.setattr(uw, "SessionLocal", explode)
    rows = [{"user_id": 7}]
    left = asyncio.run(uw._flush_batch(rows, deadline=time.monotonic() - 1))
    assert left == rows


def test_shutdown_flush_caps_pool_and_statement_timeout(monkeypatch):
    pool = type("Pool", (), {"_timeout": 30.0})()
    bind = type("Bind", (), {})()
    bind.pool = pool
    bind.dialect = type("Dialect", (), {"name": "postgresql"})()
    seen: dict = {}

    class _Result:
        def scalar(self):
            return None

    class _Session:
        def get_bind(self):
            return bind

        def connection(self):
            seen["checkout_timeout"] = pool._timeout

        def execute(self, statement, params=None):
            seen["sql"] = str(statement)
            seen["ms"] = int(params["ms"])
            return _Result()

        def bulk_insert_mappings(self, model, batch):
            seen["rows"] = len(batch)

        def commit(self):
            seen["committed"] = True

        def rollback(self):
            seen["rolled_back"] = True

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(uw, "SessionLocal", _Session)
    budget = 4.0
    left = asyncio.run(
        uw._flush_batch([{"user_id": 1}], deadline=time.monotonic() + budget)
    )
    assert left == []
    assert seen["committed"] is True
    assert seen["closed"] is True
    assert 0 < seen["checkout_timeout"] <= budget
    assert seen["checkout_timeout"] < 30
    assert pool._timeout == 30
    assert "statement_timeout" in seen["sql"]
    assert 0 < seen["ms"] <= budget * 1000


def test_checkout_does_not_wait_out_the_engine_pool_timeout(monkeypatch):
    engine = create_engine(
        "sqlite://",
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=30,
        connect_args={"check_same_thread": False},
    )
    held = engine.connect()
    try:
        monkeypatch.setattr(
            uw, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False)
        )
        started = time.monotonic()
        left = asyncio.run(
            uw._flush_batch([{"user_id": 1}], deadline=time.monotonic() + 0.4)
        )
        elapsed = time.monotonic() - started
    finally:
        held.close()
        engine.dispose()
    assert left == [{"user_id": 1}]
    assert elapsed < 2, elapsed
    assert elapsed >= 0.2, elapsed
    assert engine.pool.timeout() == 30


def test_new_database_connection_timeout_stays_within_the_drain_budget():
    params: dict = {}
    uw._connect_timeout_listener(9.7)(None, None, [], params)
    assert params["connect_timeout"] == 9
    assert params["connect_timeout"] <= 9.7
    at_two: dict = {}
    uw._connect_timeout_listener(2.0)(None, None, [], at_two)
    assert at_two["connect_timeout"] == 2
    with pytest.raises(TimeoutError, match="連線"):
        uw._connect_timeout_listener(1.2)(None, None, [], {})


def test_shutdown_logs_the_uncommitted_count(monkeypatch, caplog):
    monkeypatch.setattr(uw, "SHUTDOWN_DRAIN_SECONDS", 0.2)

    async def keep(batch, deadline=None):
        return list(batch)

    monkeypatch.setattr(uw, "_flush_batch", keep)

    async def scenario():
        try:
            await uw._usage_writer_loop()
        except asyncio.CancelledError:
            pass

    async def outer():
        uw._usage_queue = asyncio.Queue()
        uw._usage_queue.put_nowait({"user_id": 1})
        uw._usage_queue.put_nowait({"user_id": 2})
        task = asyncio.create_task(scenario())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.wait({task}, timeout=2)

    caplog.set_level(logging.ERROR, logger="app.services.usage_writer")
    asyncio.run(outer())
    assert "關閉時仍有 2 筆用量沒寫入" in caplog.text


def test_stop_waits_on_the_drain_deadline_and_logs_the_pending_count(monkeypatch, caplog):
    monkeypatch.setattr(uw, "SHUTDOWN_DRAIN_SECONDS", 0.3)
    seen: dict = {}

    async def scenario():
        uw._usage_queue = asyncio.Queue()

        async def worker():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                seen["deadline"] = uw._shutdown_deadline
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    raise

        task = asyncio.create_task(worker())
        await asyncio.sleep(0)
        uw._remember_uncommitted(3)
        started = time.monotonic()
        await uw.stop_usage_writer(task)
        seen["elapsed"] = time.monotonic() - started
        seen["done"] = task.done()
        task.cancel()
        await asyncio.wait({task}, timeout=1)

    caplog.set_level(logging.ERROR, logger="app.services.usage_writer")
    asyncio.run(scenario())
    assert seen["deadline"] is not None
    assert seen["done"] is False
    assert seen["elapsed"] < 1, seen["elapsed"]
    assert seen["elapsed"] >= 0.15, seen["elapsed"]
    assert "關閉時仍有 3 筆用量沒寫入" in caplog.text
