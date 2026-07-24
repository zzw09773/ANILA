"""Event-loop / DB deadlock guards (work order F).

Covers:
(a) runtime engine Postgres timeout options
(b) proxy_stream closure persistence off the event-loop thread
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from app.config import settings
from app.database import build_runtime_connect_args
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
