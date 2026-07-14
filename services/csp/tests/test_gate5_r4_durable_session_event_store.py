"""SQLite persistence proof for the Gate 5 SQLAlchemy SessionEventStore."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from anila_contracts import Classification, StepEvent
from anila_contracts.events import StepKind, StepStatus

from app.database import Base
from app.services.proxy.session_event_store import SqlAlchemySessionEventStore
from app.services.proxy.stream_bridge import (
    BridgeContext,
    EventBindingError,
    EventConflictError,
    EventOrderError,
    StreamBridge,
    TerminalEventError,
    TerminalConflictError,
)


def _context(
    *,
    task_id: str = "task-1",
    trace_id: str = "trace-1",
    agent_id: str = "agent-1",
    session_id: str = "session-1",
    run_id: str = "run-1",
) -> BridgeContext:
    return BridgeContext(
        task_id=task_id,
        trace_id=trace_id,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        classification=Classification.CONFIDENTIAL,
    )


def _event(
    *,
    event_id: str = "evt-1",
    sequence: int = 1,
    status: StepStatus = StepStatus.RUNNING,
    **updates: object,
) -> StepEvent:
    value = StepEvent(
        event_id=event_id,
        sequence=sequence,
        cursor="agent-cursor-is-untrusted",
        trace_id="forged-trace",
        task_id="forged-task",
        session_id="forged-session",
        invocation_id="invocation-1",
        run_id="forged-run",
        step_id="step-1",
        kind=StepKind.TOOL,
        status=status,
        agent_id="forged-agent",
        tool_name="search_documents",
        safe_input_summary="開始文件搜尋",
        classification=Classification.UNCLASSIFIED,
    ).model_dump(mode="json")
    value.update(updates)
    return StepEvent.model_validate(value)


@pytest.fixture
def store_sessions():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    first = Session()
    try:
        yield engine, Session, first
    finally:
        first.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_append_replay_survives_new_session_and_uses_server_cursor(store_sessions):
    _engine, Session, db = store_sessions
    bridge = StreamBridge(_context(), store=SqlAlchemySessionEventStore(db))

    first = bridge.append_event(_event())
    second = bridge.append_event(_event(event_id="evt-2", sequence=2))
    assert first is not None and second is not None
    assert [first.cursor, second.cursor] == [1, 2]
    db.close()

    restarted_db = Session()
    try:
        restarted = StreamBridge(
            _context(), store=SqlAlchemySessionEventStore(restarted_db)
        )
        assert [event.event_id for event in restarted.replay_events()] == [
            "evt-1",
            "evt-2",
        ]
        assert [event.cursor for event in restarted.replay_events(after_cursor=1)] == [
            "2"
        ]
    finally:
        restarted_db.close()


def test_append_replay_survives_engine_dispose_and_fresh_process_equivalent(tmp_path):
    """A file-backed SQLite DB stands in for a process restart.

    SQLite does not provide the same row-lock semantics as production
    PostgreSQL; the concurrent lock/race proof remains a PG integration test.
    This test specifically proves that no Python store object is needed for
    restart replay.
    """

    path = tmp_path / "session-events.db"
    url = f"sqlite:///{path.as_posix()}"
    engine = create_engine(url)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    first_db = Session()
    try:
        StreamBridge(
            _context(), store=SqlAlchemySessionEventStore(first_db)
        ).append_event(_event())
    finally:
        first_db.close()
        engine.dispose()

    fresh_engine = create_engine(url)
    FreshSession = sessionmaker(bind=fresh_engine)
    fresh_db = FreshSession()
    try:
        restarted = StreamBridge(
            _context(), store=SqlAlchemySessionEventStore(fresh_db)
        )
        assert [event.event_id for event in restarted.replay_events()] == ["evt-1"]
    finally:
        fresh_db.close()
        fresh_engine.dispose()


def test_duplicate_event_is_idempotent_and_changed_payload_conflicts(store_sessions):
    _engine, _Session, db = store_sessions
    bridge = StreamBridge(_context(), store=SqlAlchemySessionEventStore(db))
    first = bridge.append_event(_event())
    duplicate = bridge.append_event(_event())
    assert first is not None and duplicate is not None
    assert duplicate.duplicate is True
    assert duplicate.event == first.event

    with pytest.raises(EventConflictError):
        bridge.append_event(_event(safe_output_summary="different"))


def test_two_db_sessions_hit_same_run_event_key_deterministically(store_sessions):
    """Serialised SQLite sessions cover the unique-key retry contract.

    A PostgreSQL integration test is still required for true concurrent row
    locking; the durable uniqueness and exact duplicate/conflict behavior is
    deterministic here regardless of scheduler ordering.
    """

    _engine, Session, db = store_sessions
    first_store = SqlAlchemySessionEventStore(db)
    first = StreamBridge(_context(), store=first_store).append_event(_event())
    assert first is not None

    retry_db = Session()
    try:
        retry_store = SqlAlchemySessionEventStore(retry_db)
        retry = retry_store.append(first.event, binding=_context())
        assert retry.duplicate is True
        with pytest.raises(EventConflictError):
            retry_store.append(
                first.event.model_copy(update={"safe_output_summary": "different"}),
                binding=_context(),
            )
    finally:
        retry_db.close()


def test_terminal_retry_is_exactly_once_and_blocks_new_events(store_sessions):
    _engine, _Session, db = store_sessions
    bridge = StreamBridge(_context(), store=SqlAlchemySessionEventStore(db))
    completed_at = datetime.now(timezone.utc)
    first = bridge.append_terminal(
        StepStatus.COMPLETED,
        event_id="terminal-1",
        safe_output_summary="完成",
        completed_at=completed_at,
    )
    retry = bridge.append_terminal(
        StepStatus.COMPLETED,
        event_id="terminal-1",
        safe_output_summary="完成",
    )
    assert retry.duplicate is True
    assert retry.event == first.event
    with pytest.raises(TerminalConflictError):
        bridge.append_terminal(
            StepStatus.COMPLETED,
            event_id="terminal-1",
            safe_output_summary="不同",
        )
    with pytest.raises(TerminalEventError):
        bridge.append_event(_event(event_id="after-terminal", sequence=2))


def test_binding_isolation_applies_to_replay_and_append(store_sessions):
    _engine, _Session, db = store_sessions
    context = _context()
    bridge = StreamBridge(context, store=SqlAlchemySessionEventStore(db))
    bridge.append_event(_event())
    wrong = StreamBridge(
        _context(task_id="other-task"),
        store=SqlAlchemySessionEventStore(db),
    )

    with pytest.raises(EventBindingError):
        wrong.replay_events()
    with pytest.raises(EventBindingError):
        wrong.append_event(_event())


def test_source_sequence_is_monotonic_but_forged_cursor_is_ignored(store_sessions):
    _engine, _Session, db = store_sessions
    bridge = StreamBridge(_context(), store=SqlAlchemySessionEventStore(db))
    bridge.append_event(_event(sequence=3))
    with pytest.raises(EventOrderError):
        bridge.append_event(_event(event_id="evt-2", sequence=2))
    assert bridge.replay_events()[0].cursor == "1"
