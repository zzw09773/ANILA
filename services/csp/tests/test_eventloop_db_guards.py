"""Event-loop / DB deadlock guards (work order F).

Covers:
(a) runtime engine Postgres timeout options
(b) proxy_stream closure persistence off the event-loop thread
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base, SessionLocal, build_runtime_connect_args
from app.models.department import Department
from app.services.proxy import service as proxy_service


def test_runtime_connect_args_include_lock_and_idle_tx_timeouts(monkeypatch):
    monkeypatch.setattr(settings, "ANILA_DB_LOCK_TIMEOUT_MS", 5000)
    monkeypatch.setattr(settings, "ANILA_DB_IDLE_TX_TIMEOUT_MS", 60_000)
    args = build_runtime_connect_args("postgresql://csp:x@localhost:5432/csp")
    options = args["options"]
    assert "lock_timeout=5000" in options
    assert "idle_in_transaction_session_timeout=60000" in options


def test_runtime_connect_args_skip_non_postgres():
    assert build_runtime_connect_args("sqlite:////tmp/csp.db") == {}


def test_session_factory_does_not_expire_on_commit():
    """``expire_on_commit`` must stay False — it is a deadlock guard.

    With the SQLAlchemy default (True), every ``commit()`` arms all ORM
    instances for a lazy re-SELECT.  On an ``async def`` handler or inside a
    StreamingResponse body the next attribute touch then runs synchronous DB
    I/O on the event-loop thread AND re-opens a transaction that keeps its
    pooled connection until the response finishes — the ``_load_expired`` →
    ``QueuePool limit of size 10 overflow 20 reached`` ring behind the
    2026-07-25 platform-wide freeze (``pg_active=31, idle_tx=30``).

    It is also what makes releasing the streaming admission Session safe:
    ``_release_stream_admission_session`` detaches the admission objects, and
    a detached instance whose attributes were expired at commit raises
    ``DetachedInstanceError`` instead of returning its already-loaded value.
    """
    assert SessionLocal.kw["expire_on_commit"] is False


def test_expired_orm_touch_after_commit_would_repin_a_connection():
    """Pins *why* the flag matters, so a future flip is caught with a reason.

    Same Session, same commit, only ``expire_on_commit`` differs: the default
    checks a connection back out and leaves a transaction open, which is
    exactly the per-stream leak that exhausted the pool.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine, tables=[Department.__table__])
    outstanding = {"n": 0}

    @event.listens_for(engine, "checkout")
    def _checkout(*_args):  # pragma: no cover - event hook
        outstanding["n"] += 1

    @event.listens_for(engine, "checkin")
    def _checkin(*_args):  # pragma: no cover - event hook
        outstanding["n"] -= 1

    def touch_after_commit(*, expire_on_commit: bool) -> tuple[int, bool]:
        session = sessionmaker(bind=engine, expire_on_commit=expire_on_commit)()
        try:
            row = Department(name=f"dept-{expire_on_commit}")
            session.add(row)
            session.commit()
            assert outstanding["n"] == 0
            _ = row.name  # the deferred read a streaming exit performs
            return outstanding["n"], session.in_transaction()
        finally:
            session.close()

    assert touch_after_commit(expire_on_commit=True) == (1, True)
    assert touch_after_commit(expire_on_commit=False) == (0, False)


def test_runtime_engine_uses_builder_for_postgres_url(monkeypatch):
    """When DATABASE_URL is Postgres, engine.connect_args must carry options."""
    monkeypatch.setattr(settings, "ANILA_DB_LOCK_TIMEOUT_MS", 4123)
    monkeypatch.setattr(settings, "ANILA_DB_IDLE_TX_TIMEOUT_MS", 78_000)
    # Re-evaluate builder against a Postgres URL (live test engine is SQLite).
    args = build_runtime_connect_args("postgresql://csp_app@db/csp")
    assert args == {
        "options": (
            "-c lock_timeout=4123 "
            "-c idle_in_transaction_session_timeout=78000"
        )
    }


@pytest.mark.asyncio
async def test_proxy_stream_persist_runs_off_event_loop(monkeypatch):
    loop_ident = threading.get_ident()
    seen: dict[str, object] = {}

    async def fake_upstream(**_kwargs):
        yield "data: {\"choices\":[]}\n\n"
        yield "data: [DONE]\n\n"

    def capture_persist(_db, closure):
        seen["thread_ident"] = threading.get_ident()
        seen["thread_name"] = threading.current_thread().name
        seen["closure_status"] = closure.status
        return 99

    monkeypatch.setattr(proxy_service, "_proxy_stream_impl", fake_upstream)
    monkeypatch.setattr(proxy_service, "_require_pilot_sink_admission", lambda **_: None)
    monkeypatch.setattr(proxy_service, "_lock_task_run_admission", lambda **_: "無機密")
    monkeypatch.setattr(proxy_service, "_lock_registry_admission", lambda **_: None)
    monkeypatch.setattr(proxy_service, "_commit_stream_admission", lambda _db: None)
    monkeypatch.setattr(proxy_service, "persist_task_call_closure", capture_persist)

    class _DB:
        def rollback(self):
            pass

    chunks: list[str] = []
    async for chunk in proxy_service.proxy_stream(
        target_url="http://mock-llm/v1/chat/completions",
        api_key_id=1,
        user_id=2,
        department_id=None,
        usage_model_id=3,
        request_body={"model": "m", "messages": []},
        task_id=10,
        task_trace_id="trace-10",
        task_run_id=11,
        task_run_started_at=datetime.now(timezone.utc),
        governance_db=_DB(),
        admitted_classification_level="無機密",
    ):
        chunks.append(chunk)

    assert chunks
    assert seen["closure_status"] == "completed"
    assert seen["thread_ident"] != loop_ident
    assert seen["thread_name"] != "MainThread"


@pytest.mark.asyncio
async def test_proxy_stream_persist_failure_is_logged(monkeypatch, caplog):
    async def fake_upstream(**_kwargs):
        yield "data: {\"choices\":[]}\n\n"

    def boom(_db, _closure):
        raise RuntimeError("simulated closure failure")

    monkeypatch.setattr(proxy_service, "_proxy_stream_impl", fake_upstream)
    monkeypatch.setattr(proxy_service, "_require_pilot_sink_admission", lambda **_: None)
    monkeypatch.setattr(proxy_service, "_lock_task_run_admission", lambda **_: "無機密")
    monkeypatch.setattr(proxy_service, "_lock_registry_admission", lambda **_: None)
    monkeypatch.setattr(proxy_service, "_commit_stream_admission", lambda _db: None)
    monkeypatch.setattr(proxy_service, "persist_task_call_closure", boom)

    class _DB:
        def rollback(self):
            pass

    # Match service.py logger name (``app.services.proxy_service``), not
    # the package module path ``app.services.proxy.service``.
    with caplog.at_level("ERROR", logger="app.services.proxy_service"):
        # Success-path persist fails → exception; finally retries and logs again.
        # Drain must not die silently (no GeneratorExit without log).
        agen = proxy_service.proxy_stream(
            target_url="http://mock-llm/v1/chat/completions",
            api_key_id=1,
            user_id=2,
            department_id=None,
            usage_model_id=3,
            request_body={"model": "m", "messages": []},
            task_id=10,
            task_trace_id="trace-10",
            task_run_id=11,
            governance_db=_DB(),
            admitted_classification_level="無機密",
        )
        with pytest.raises(RuntimeError, match="simulated closure failure"):
            async for _ in agen:
                pass

    assert any(
        "durable closure" in record.getMessage() for record in caplog.records
    )
    assert any(record.exc_info for record in caplog.records)
