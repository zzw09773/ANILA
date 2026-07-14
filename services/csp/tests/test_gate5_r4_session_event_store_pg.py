"""True PostgreSQL race proofs for the Gate 5 durable session-event store.

The SQLite tests cover the adapter's ordinary persistence contract.  These
tests deliberately use two independent SQLAlchemy engines, sessions and
threads so that PostgreSQL row locks and the database uniqueness constraints
are exercised as they are in the CSP deployment.

The suite is opt-in through ``TEST_DATABASE_URL`` (or ``DATABASE_URL``) and
never creates or migrates the schema.  It therefore cannot accidentally run
against a local SQLite test database or alter a developer's database.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from anila_contracts import Classification, StepEvent
from anila_contracts.events import StepKind, StepStatus

from app.models.session_event import SessionEvent, SessionEventRun
from app.services.proxy.session_event_store import SqlAlchemySessionEventStore
from app.services.proxy.stream_bridge import (
    BridgeContext,
    EventOrderError,
    SessionAppendResult,
    TerminalEventError,
)

pytestmark = pytest.mark.integration


def _context(run_id: str) -> BridgeContext:
    return BridgeContext(
        task_id=f"pg-task-{run_id}",
        trace_id=f"pg-trace-{run_id}",
        agent_id="pg-agent-gate5",
        session_id=f"pg-session-{run_id}",
        run_id=run_id,
        classification=Classification.CONFIDENTIAL,
    )


def _event(
    *,
    event_id: str,
    sequence: int,
    status: StepStatus = StepStatus.RUNNING,
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
    return StepEvent.model_validate(value)


def _bound(event: StepEvent, binding: BridgeContext) -> StepEvent:
    """Apply the trusted CSP binding before calling the store directly."""

    return event.model_copy(
        update={
            "task_id": binding.task_id,
            "trace_id": binding.trace_id,
            "agent_id": binding.agent_id,
            "session_id": binding.session_id,
            "run_id": binding.run_id,
            "classification": binding.classification,
        }
    )


@pytest.fixture(scope="module")
def postgres_database_url() -> str:
    """Return an explicitly configured PostgreSQL URL, or skip the module."""

    raw_url = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not raw_url:
        pytest.skip("TEST_DATABASE_URL/DATABASE_URL 未設定，略過 PostgreSQL 整合測試")

    try:
        parsed = make_url(raw_url)
    except Exception as exc:  # SQLAlchemy's URL parser has driver-specific errors.
        pytest.skip(f"PostgreSQL URL 無法解析：{type(exc).__name__}")
    dialect = parsed.drivername.split("+", 1)[0].lower()
    if dialect not in {"postgres", "postgresql"}:
        pytest.skip("此檔只接受 PostgreSQL TEST_DATABASE_URL/DATABASE_URL")

    engine = create_engine(parsed, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
            tables = inspect(connection)
            if not tables.has_table("session_event_runs") or not tables.has_table(
                "session_events"
            ):
                pytest.skip("PostgreSQL 尚未套用 r1_0026 session-event schema")
    except SQLAlchemyError as exc:
        pytest.skip(f"PostgreSQL 不可連線：{type(exc).__name__}")
    finally:
        engine.dispose()
    return raw_url


def _append_worker(
    database_url: str,
    binding: BridgeContext,
    event: StepEvent,
    barrier: Barrier,
) -> object:
    """Append from one engine/session pair and return errors for assertions."""

    engine = create_engine(database_url, pool_pre_ping=True)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        barrier.wait(timeout=30)
        return SqlAlchemySessionEventStore(db).append(event, binding=binding)
    except Exception as exc:  # The caller asserts the allowed race outcomes.
        return exc
    finally:
        db.close()
        engine.dispose()


def _race(
    database_url: str,
    binding: BridgeContext,
    events: tuple[StepEvent, ...],
) -> list[object]:
    barrier = Barrier(len(events))
    with ThreadPoolExecutor(max_workers=len(events)) as executor:
        futures = [
            executor.submit(_append_worker, database_url, binding, event, barrier)
            for event in events
        ]
        return [future.result(timeout=60) for future in futures]


def _successful(results: list[object]) -> list[SessionAppendResult]:
    unexpected = [
        f"{type(result).__name__}: {result}"
        for result in results
        if not isinstance(result, SessionAppendResult)
    ]
    assert not unexpected, "unexpected PostgreSQL append result(s): " + "; ".join(
        unexpected
    )
    return [cast(SessionAppendResult, result) for result in results]


def _read_ledger(database_url: str, binding: BridgeContext) -> dict[str, object]:
    """Read all assertions through a fresh engine/session pair."""

    engine = create_engine(database_url, pool_pre_ping=True)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        run = (
            db.query(SessionEventRun)
            .filter(SessionEventRun.run_id == binding.run_id)
            .one_or_none()
        )
        rows = (
            db.query(SessionEvent)
            .filter(SessionEvent.run_id == binding.run_id)
            .order_by(SessionEvent.cursor.asc())
            .all()
        )
        store = SqlAlchemySessionEventStore(db)
        replay = store.all_events(binding=binding)
        terminal = store.terminal(binding=binding)
        return {
            "next_cursor": None if run is None else int(run.next_cursor),
            "last_source_sequence": (None if run is None else run.last_source_sequence),
            "terminal_event_id": None if run is None else run.terminal_event_id,
            "rows": tuple(
                (
                    str(row.event_id),
                    int(row.cursor),
                    int(row.source_sequence),
                    bool(row.is_terminal),
                )
                for row in rows
            ),
            "replay": tuple((event.event_id, event.cursor) for event in replay),
            "terminal": None if terminal is None else terminal.event_id,
        }
    finally:
        db.close()
        engine.dispose()


def _cleanup(database_url: str, run_id: str) -> None:
    """Delete only this test's random run after assertions finish."""

    engine = create_engine(database_url, pool_pre_ping=True)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.query(SessionEvent).filter(SessionEvent.run_id == run_id).delete(
            synchronize_session=False
        )
        db.query(SessionEventRun).filter(SessionEventRun.run_id == run_id).delete(
            synchronize_session=False
        )
        db.commit()
    finally:
        db.close()
        engine.dispose()


def _assert_contiguous_rows(rows: tuple[tuple[str, int, int, bool], ...]) -> None:
    cursors = [cursor for _event_id, cursor, _sequence, _terminal in rows]
    assert len(cursors) == len(set(cursors)), "duplicate durable cursor"
    assert cursors == list(range(1, len(cursors) + 1))


def test_postgres_same_event_race_is_exactly_once_and_replays_after_restart(
    postgres_database_url: str,
) -> None:
    run_id = f"pg-r4-same-{uuid4().hex}"
    binding = _context(run_id)
    event = _bound(_event(event_id=f"{run_id}-event", sequence=1), binding)
    try:
        results = _race(postgres_database_url, binding, (event, event))
        appended = _successful(results)

        assert sum(result.duplicate is False for result in appended) == 1
        assert sum(result.duplicate is True for result in appended) == 1
        assert {result.cursor for result in appended} == {1}

        ledger = _read_ledger(postgres_database_url, binding)
        assert ledger["next_cursor"] == 1
        assert ledger["last_source_sequence"] == 1
        assert ledger["terminal_event_id"] is None
        assert ledger["rows"] == ((event.event_id, 1, 1, False),)

        # _read_ledger uses a newly-created engine/session, proving replay does
        # not depend on either racing worker's Python objects.
        assert ledger["replay"] == ((event.event_id, "1"),)
        assert ledger["terminal"] is None
    finally:
        _cleanup(postgres_database_url, run_id)


def test_postgres_different_source_sequence_race_never_duplicates_cursor(
    postgres_database_url: str,
) -> None:
    run_id = f"pg-r4-order-{uuid4().hex}"
    binding = _context(run_id)
    first = _bound(_event(event_id=f"{run_id}-sequence-1", sequence=1), binding)
    second = _bound(_event(event_id=f"{run_id}-sequence-2", sequence=2), binding)
    try:
        results = _race(postgres_database_url, binding, (first, second))
        successes = [
            cast(SessionAppendResult, result)
            for result in results
            if isinstance(result, SessionAppendResult)
        ]
        errors = [result for result in results if isinstance(result, Exception)]

        # Depending on which worker obtains the run lock first, both events may
        # commit in source order, or the lower sequence is rejected after the
        # higher sequence committed.  Neither outcome may corrupt the ledger.
        assert successes
        assert all(isinstance(error, EventOrderError) for error in errors)

        ledger = _read_ledger(postgres_database_url, binding)
        rows = cast(tuple[tuple[str, int, int, bool], ...], ledger["rows"])
        _assert_contiguous_rows(rows)
        assert ledger["next_cursor"] == len(rows)
        assert ledger["terminal_event_id"] is None
        assert ledger["replay"] == tuple(
            (event_id, str(cursor)) for event_id, cursor, *_ in rows
        )
        assert [sequence for _event_id, _cursor, sequence, _terminal in rows] == sorted(
            sequence for _event_id, _cursor, sequence, _terminal in rows
        )
        assert {event_id for event_id, *_ in rows} <= {first.event_id, second.event_id}
    finally:
        _cleanup(postgres_database_url, run_id)


def test_postgres_terminal_latch_survives_duplicate_and_new_event_race(
    postgres_database_url: str,
) -> None:
    run_id = f"pg-r4-terminal-{uuid4().hex}"
    binding = _context(run_id)
    terminal = _bound(
        _event(
            event_id=f"{run_id}-terminal",
            sequence=1,
            status=StepStatus.COMPLETED,
        ),
        binding,
    )
    after_terminal = _bound(
        _event(
            event_id=f"{run_id}-after-terminal",
            sequence=2,
            status=StepStatus.RUNNING,
        ),
        binding,
    )
    try:
        # Seed the terminal latch once, then race an exact retry against a new
        # event with a different source sequence.
        seed_engine = create_engine(postgres_database_url, pool_pre_ping=True)
        SeedSession = sessionmaker(bind=seed_engine)
        seed_db = SeedSession()
        try:
            seeded = SqlAlchemySessionEventStore(seed_db).append(
                terminal, binding=binding
            )
            assert seeded.duplicate is False
        finally:
            seed_db.close()
            seed_engine.dispose()

        results = _race(postgres_database_url, binding, (terminal, after_terminal))
        duplicate_retries = [
            result for result in results if isinstance(result, SessionAppendResult)
        ]
        terminal_errors = [
            result for result in results if isinstance(result, TerminalEventError)
        ]
        assert len(duplicate_retries) == 1
        assert duplicate_retries[0].duplicate is True
        assert len(terminal_errors) == 1

        ledger = _read_ledger(postgres_database_url, binding)
        rows = cast(tuple[tuple[str, int, int, bool], ...], ledger["rows"])
        _assert_contiguous_rows(rows)
        assert rows == ((terminal.event_id, 1, 1, True),)
        assert ledger["next_cursor"] == 1
        assert ledger["last_source_sequence"] == 1
        assert ledger["terminal_event_id"] == terminal.event_id
        assert ledger["replay"] == ((terminal.event_id, "1"),)
        assert ledger["terminal"] == terminal.event_id
    finally:
        _cleanup(postgres_database_url, run_id)
