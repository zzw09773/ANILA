"""Regression: health loops must not touch SQLAlchemy on the asyncio event loop.

Production incident: an open ORM transaction was held across await HTTP probes,
then ``db.commit()`` blocked on a row lock on the event loop and froze CSP.

These tests assert:
(a) SessionLocal create / query / commit / close never run on the event-loop thread
(b) no session remains open while a probe await is in flight
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from app.services import health_checker


class _RecordingSession:
    """Minimal session stub that records thread identity for DB operations."""

    def __init__(self, rows, events: list, open_sessions: set):
        self._rows = rows
        self._events = events
        self._open_sessions = open_sessions
        tid = threading.get_ident()
        self._events.append(("create", tid))
        self._open_sessions.add(self)

    def query(self, _model_cls):
        self._events.append(("query", threading.get_ident()))
        return _RecordingQuery(self._rows)

    def commit(self):
        self._events.append(("commit", threading.get_ident()))

    def close(self):
        self._events.append(("close", threading.get_ident()))
        self._open_sessions.discard(self)


class _RecordingQuery:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


def _install_session_local(monkeypatch, rows, events, open_sessions):
    def _session_local():
        return _RecordingSession(rows, events, open_sessions)

    monkeypatch.setattr(health_checker, "SessionLocal", _session_local)


async def _cancel_after_one_sleep(_seconds):
    raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_model_health_loop_keeps_db_off_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    events: list[tuple[str, int]] = []
    open_sessions: set = set()
    open_during_probe: list[int] = []

    model = SimpleNamespace(
        id=1,
        name="m1",
        display_name="Model One",
        endpoint_url="http://model.test",
        health_status="unknown",
        health_checked_at=None,
        is_active=True,
    )
    _install_session_local(monkeypatch, [model], events, open_sessions)

    async def probe(model_id, endpoint_url):
        open_during_probe.append(len(open_sessions))
        assert threading.get_ident() == loop_thread
        assert model_id == 1
        assert endpoint_url == "http://model.test"
        return health_checker.HEALTH_HEALTHY

    monkeypatch.setattr(health_checker, "check_model_health", probe)
    monkeypatch.setattr(health_checker, "admit_registry_provider", lambda _m: None)
    monkeypatch.setattr(health_checker, "upsert_alert", lambda *_a, **_k: None)
    monkeypatch.setattr(
        health_checker, "resolve_alert_by_fingerprint", lambda *_a, **_k: None
    )
    monkeypatch.setattr(health_checker.asyncio, "sleep", _cancel_after_one_sleep)

    with pytest.raises(asyncio.CancelledError):
        await health_checker._health_check_loop()

    assert events, "expected SessionLocal activity"
    for op, tid in events:
        assert tid != loop_thread, f"{op} ran on the event-loop thread"
    assert open_during_probe == [0], (
        "session must be closed before probe await; "
        f"saw open counts {open_during_probe}"
    )


@pytest.mark.asyncio
async def test_agent_health_loop_keeps_db_off_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    events: list[tuple[str, int]] = []
    open_sessions: set = set()
    open_during_probe: list[int] = []

    agent = SimpleNamespace(
        id=7,
        name="a1",
        endpoint_url="http://agent.test",
        health_status="unknown",
        health_checked_at=None,
        approval_status="approved",
    )
    _install_session_local(monkeypatch, [agent], events, open_sessions)

    async def probe(agent_id, endpoint_url):
        open_during_probe.append(len(open_sessions))
        assert threading.get_ident() == loop_thread
        assert agent_id == 7
        assert endpoint_url == "http://agent.test"
        return health_checker.HEALTH_UNHEALTHY

    monkeypatch.setattr(health_checker, "check_agent_health", probe)
    monkeypatch.setattr(health_checker, "upsert_alert", lambda *_a, **_k: None)
    monkeypatch.setattr(
        health_checker, "resolve_alert_by_fingerprint", lambda *_a, **_k: None
    )
    monkeypatch.setattr(health_checker.asyncio, "sleep", _cancel_after_one_sleep)

    with pytest.raises(asyncio.CancelledError):
        await health_checker._agent_health_check_loop()

    assert events, "expected SessionLocal activity"
    for op, tid in events:
        assert tid != loop_thread, f"{op} ran on the event-loop thread"
    assert open_during_probe == [0], (
        "session must be closed before probe await; "
        f"saw open counts {open_during_probe}"
    )


@pytest.mark.asyncio
async def test_model_write_back_skips_row_changed_during_probe(monkeypatch):
    """A probe result must not be written onto a row whose endpoint changed
    (or that was deactivated) between snapshot and write-back — the result
    describes the OLD configuration and would pollute the new one."""
    alerts: list = []
    model = SimpleNamespace(
        id=1,
        name="m1",
        display_name="Model One",
        endpoint_url="http://old.test",
        health_status="unknown",
        health_checked_at=None,
        is_active=True,
    )
    _install_session_local(monkeypatch, [model], [], set())

    async def probe(_model_id, endpoint_url):
        assert endpoint_url == "http://old.test"
        # Admin swaps the endpoint mid-probe.
        model.endpoint_url = "http://new.test"
        return health_checker.HEALTH_UNHEALTHY

    monkeypatch.setattr(health_checker, "check_model_health", probe)
    monkeypatch.setattr(health_checker, "admit_registry_provider", lambda _m: None)
    monkeypatch.setattr(
        health_checker, "upsert_alert", lambda *_a, **k: alerts.append(k)
    )
    monkeypatch.setattr(
        health_checker, "resolve_alert_by_fingerprint", lambda *_a, **_k: None
    )
    monkeypatch.setattr(health_checker.asyncio, "sleep", _cancel_after_one_sleep)

    with pytest.raises(asyncio.CancelledError):
        await health_checker._health_check_loop()

    assert model.health_status == "unknown", "stale result written onto changed row"
    assert model.health_checked_at is None
    assert alerts == [], "alert raised for a row that was reconfigured mid-probe"


@pytest.mark.asyncio
async def test_agent_write_back_skips_row_revoked_during_probe(monkeypatch):
    """An agent whose approval was revoked between snapshot and write-back must
    receive neither a health result nor an alert."""
    alerts: list = []
    agent = SimpleNamespace(
        id=7,
        name="a1",
        endpoint_url="http://agent.test",
        health_status="unknown",
        health_checked_at=None,
        approval_status="approved",
    )
    _install_session_local(monkeypatch, [agent], [], set())

    async def probe(_agent_id, _endpoint_url):
        agent.approval_status = "revoked"
        return health_checker.HEALTH_UNHEALTHY

    monkeypatch.setattr(health_checker, "check_agent_health", probe)
    monkeypatch.setattr(
        health_checker, "upsert_alert", lambda *_a, **k: alerts.append(k)
    )
    monkeypatch.setattr(
        health_checker, "resolve_alert_by_fingerprint", lambda *_a, **_k: None
    )
    monkeypatch.setattr(health_checker.asyncio, "sleep", _cancel_after_one_sleep)

    with pytest.raises(asyncio.CancelledError):
        await health_checker._agent_health_check_loop()

    assert agent.health_status == "unknown"
    assert agent.health_checked_at is None
    assert alerts == []


@pytest.mark.asyncio
async def test_task_reconciliation_loop_runs_db_off_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    events: list[tuple[str, int]] = []
    open_sessions: set = set()
    reconcile_threads: list[int] = []

    _install_session_local(monkeypatch, [], events, open_sessions)

    def fake_reconcile(db, *, stale_after_seconds):
        reconcile_threads.append(threading.get_ident())
        assert stale_after_seconds == health_checker.settings.TASK_RUN_STALE_SECONDS
        return 0

    monkeypatch.setattr(
        "app.services.proxy.task_link.reconcile_stale_task_runs",
        fake_reconcile,
    )
    monkeypatch.setattr(health_checker.asyncio, "sleep", _cancel_after_one_sleep)

    with pytest.raises(asyncio.CancelledError):
        await health_checker._task_reconciliation_loop()

    assert events, "expected SessionLocal activity"
    for op, tid in events:
        assert tid != loop_thread, f"{op} ran on the event-loop thread"
    assert reconcile_threads and all(t != loop_thread for t in reconcile_threads)
