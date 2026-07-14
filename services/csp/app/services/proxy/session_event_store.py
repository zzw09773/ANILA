"""SQLAlchemy-backed durable implementation of the Gate 5 event-store port.

``StreamBridge`` owns wire validation and rebinding; this adapter owns the
durable cursor, append-only event rows and idempotency/terminal latches.  Each
append runs in one database transaction and commits the event before returning
so a fresh CSP session (or a restarted process) can replay the same run.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, cast

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from anila_contracts import StepEvent
from anila_contracts.events import StepStatus

from app.models.session_event import SessionEvent, SessionEventRun
from app.services.proxy.stream_bridge import (
    BridgeContext,
    EventBindingError,
    EventConflictError,
    EventOrderError,
    SessionAppendResult,
    SessionEventStore as SessionEventStoreProtocol,
    TerminalEventError,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SqlAlchemySessionEventStore(SessionEventStoreProtocol):
    """Durable CSP ``SessionEventStore`` backed by one SQLAlchemy Session.

    The caller supplies a normal synchronous SQLAlchemy ``Session``.  The
    adapter commits successful appends (including exact duplicate retries) and
    rolls back failed operations, making the event visible to a newly-created
    session without requiring the caller to know the store's transaction
    internals.
    """

    durable = True

    def __init__(self, db: Session) -> None:
        self.db = db

    @staticmethod
    def _binding_key(binding: BridgeContext) -> tuple[str, str, str, str, str]:
        return (
            binding.task_id,
            binding.run_id,
            binding.session_id,
            binding.trace_id,
            binding.agent_id,
        )

    @classmethod
    def _assert_event_binding(cls, event: StepEvent, binding: BridgeContext) -> None:
        values = {
            "task_id": (event.task_id, binding.task_id),
            "trace_id": (event.trace_id, binding.trace_id),
            "agent_id": (event.agent_id, binding.agent_id),
            "session_id": (event.session_id, binding.session_id),
            "run_id": (event.run_id, binding.run_id),
            "classification": (event.classification, binding.classification),
        }
        mismatches = [
            name for name, (actual, expected) in values.items() if actual != expected
        ]
        if mismatches:
            raise EventBindingError(
                "StepEvent 治理欄位未綁定 trusted dispatch context: "
                + ",".join(mismatches),
                run_id=binding.run_id,
                event_id=event.event_id,
            )

    @staticmethod
    def _event_digest(event: StepEvent) -> str:
        """Hash all producer payload fields except the CSP-assigned cursor."""

        payload = cast(dict[str, Any], event.model_dump(mode="json"))
        payload.pop("cursor", None)
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _row_binding(row: SessionEventRun | SessionEvent) -> tuple[str, str, str, str, str]:
        return (
            cast(str, row.task_id),
            cast(str, row.run_id),
            cast(str, row.session_id),
            cast(str, row.trace_id),
            cast(str, row.agent_id),
        )

    @classmethod
    def _assert_row_binding(
        cls,
        row: SessionEventRun | SessionEvent,
        binding: BridgeContext,
        *,
        event_id: str | None = None,
    ) -> None:
        if cls._row_binding(row) != cls._binding_key(binding):
            raise EventBindingError(
                "run_id 已綁定其他 task/session/trace/agent",
                run_id=binding.run_id,
                event_id=event_id,
            )

    def _run_for_update(self, binding: BridgeContext) -> SessionEventRun | None:
        return cast(
            SessionEventRun | None,
            self.db.query(SessionEventRun)
            .filter(SessionEventRun.run_id == binding.run_id)
            .populate_existing()
            .with_for_update()
            .one_or_none(),
        )

    def _get_or_create_run(self, binding: BridgeContext) -> SessionEventRun:
        run = self._run_for_update(binding)
        if run is not None:
            self._assert_row_binding(run, binding)
            return run

        run = SessionEventRun(
            run_id=binding.run_id,
            task_id=binding.task_id,
            trace_id=binding.trace_id,
            agent_id=binding.agent_id,
            session_id=binding.session_id,
            next_cursor=0,
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        self.db.add(run)
        try:
            self.db.flush()
        except IntegrityError:
            # Another worker may have created this run between our read and
            # insert.  Roll back the failed INSERT, then lock/read its binding
            # in a fresh transaction; a mismatched binding remains fail-closed.
            self.db.rollback()
            existing = self._run_for_update(binding)
            if existing is None:
                raise
            self._assert_row_binding(existing, binding)
            return existing
        return run

    def claim_dispatch(self, *, binding: BridgeContext) -> bool:
        """Durably claim a not-yet-started invocation before Agent I/O.

        The row lock is intentionally held by the caller's SQLAlchemy
        transaction until the first event/terminal append commits.  A second
        CSP process therefore waits for the winner, then observes either the
        durable cursor or terminal latch and replays instead of issuing a
        second outbound call.  If the winner crashes before an append, the
        transaction rolls back; the downstream idempotency key is still sent
        on the retry so the Agent can return its durable result.
        """

        run = self._run_for_update(binding)
        if run is None:
            run = SessionEventRun(
                run_id=binding.run_id,
                task_id=binding.task_id,
                trace_id=binding.trace_id,
                agent_id=binding.agent_id,
                session_id=binding.session_id,
                next_cursor=0,
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
            self.db.add(run)
            try:
                self.db.flush()
            except IntegrityError:
                self.db.rollback()
                existing = self._run_for_update(binding)
                if existing is None:
                    raise
                self._assert_row_binding(existing, binding)
                run = existing
        else:
            self._assert_row_binding(run, binding)
        return run.terminal_event_id is None and int(run.next_cursor or 0) == 0

    @staticmethod
    def _event_from_row(row: SessionEvent, *, binding: BridgeContext) -> StepEvent:
        try:
            event = StepEvent.model_validate(row.payload)
        except (ValidationError, TypeError, ValueError) as exc:
            raise EventBindingError(
                "durable session event payload 無法重新驗證",
                run_id=binding.run_id,
                event_id=cast(str, row.event_id),
            ) from exc
        if int(event.cursor) != int(row.cursor):
            raise EventBindingError(
                "durable session event cursor 不一致",
                run_id=binding.run_id,
                event_id=event.event_id,
            )
        SqlAlchemySessionEventStore._assert_event_binding(event, binding)
        SqlAlchemySessionEventStore._assert_row_binding(
            row, binding, event_id=event.event_id
        )
        return event

    @staticmethod
    def _cursor(value: int | str) -> int:
        if isinstance(value, bool):
            raise ValueError("after_cursor 必須是非負整數")
        try:
            cursor = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("after_cursor 必須是非負整數") from exc
        if cursor < 0:
            raise ValueError("after_cursor 不得小於 0")
        return cursor

    def append(
        self, event: StepEvent, *, binding: BridgeContext
    ) -> SessionAppendResult:
        if not isinstance(event, StepEvent):
            raise TypeError("event 必須是 StepEvent")
        self._assert_event_binding(event, binding)
        digest = self._event_digest(event)
        try:
            run = self._get_or_create_run(binding)
            existing = (
                self.db.query(SessionEvent)
                .filter(
                    SessionEvent.run_id == binding.run_id,
                    SessionEvent.event_id == event.event_id,
                )
                .populate_existing()
                .with_for_update()
                .one_or_none()
            )
            if existing is not None:
                self._assert_row_binding(existing, binding, event_id=event.event_id)
                if existing.payload_sha256 != digest:
                    raise EventConflictError(
                        "相同 (run_id,event_id) 的 payload 不一致",
                        run_id=binding.run_id,
                        event_id=event.event_id,
                    )
                replayed = self._event_from_row(existing, binding=binding)
                self.db.commit()
                return SessionAppendResult(replayed, duplicate=True)

            if run.terminal_event_id is not None:
                raise TerminalEventError(
                    "run 已寫入 terminal event，不接受後續事件",
                    run_id=binding.run_id,
                    event_id=event.event_id,
                )

            source_sequence = int(event.sequence)
            if (
                run.last_source_sequence is not None
                and source_sequence <= int(run.last_source_sequence)
            ):
                raise EventOrderError(
                    "新事件 cursor/sequence 必須嚴格遞增",
                    run_id=binding.run_id,
                    event_id=event.event_id,
                )

            cursor = int(run.next_cursor) + 1
            persisted = event.model_copy(update={"cursor": str(cursor)})
            payload = cast(dict[str, Any], persisted.model_dump(mode="json"))
            is_terminal = persisted.status in {
                StepStatus.COMPLETED,
                StepStatus.FAILED,
                StepStatus.CANCELLED,
            }
            row = SessionEvent(
                run_id=binding.run_id,
                task_id=binding.task_id,
                trace_id=binding.trace_id,
                agent_id=binding.agent_id,
                session_id=binding.session_id,
                event_id=persisted.event_id,
                cursor=cursor,
                source_sequence=source_sequence,
                status=persisted.status.value,
                is_terminal=is_terminal,
                payload_sha256=digest,
                payload=payload,
                created_at=_utcnow(),
            )
            self.db.add(row)
            run.next_cursor = cursor
            run.last_source_sequence = source_sequence
            run.updated_at = _utcnow()
            if is_terminal:
                run.terminal_event_id = persisted.event_id
            self.db.flush()
            self.db.commit()
            return SessionAppendResult(persisted, duplicate=False)
        except Exception:
            self.db.rollback()
            raise

    def _run_for_read(self, binding: BridgeContext) -> SessionEventRun | None:
        run = cast(
            SessionEventRun | None,
            self.db.query(SessionEventRun)
            .filter(SessionEventRun.run_id == binding.run_id)
            .populate_existing()
            .one_or_none(),
        )
        if run is not None:
            self._assert_row_binding(run, binding)
        return run

    def read_after(
        self, *, binding: BridgeContext, after_cursor: int | str = 0
    ) -> tuple[StepEvent, ...]:
        try:
            cursor = self._cursor(after_cursor)
        except ValueError as exc:
            raise EventOrderError(str(exc), run_id=binding.run_id) from exc
        run = self._run_for_read(binding)
        if run is None:
            return ()
        rows = (
            self.db.query(SessionEvent)
            .filter(
                SessionEvent.run_id == binding.run_id,
                SessionEvent.cursor > cursor,
            )
            .order_by(SessionEvent.cursor.asc())
            .all()
        )
        return tuple(self._event_from_row(row, binding=binding) for row in rows)

    def terminal(self, *, binding: BridgeContext) -> StepEvent | None:
        run = self._run_for_read(binding)
        if run is None or run.terminal_event_id is None:
            return None
        row = (
            self.db.query(SessionEvent)
            .filter(
                SessionEvent.run_id == binding.run_id,
                SessionEvent.event_id == run.terminal_event_id,
            )
            .one_or_none()
        )
        if row is None:
            raise EventBindingError(
                "durable run terminal latch 找不到對應事件",
                run_id=binding.run_id,
                event_id=cast(str, run.terminal_event_id),
            )
        return self._event_from_row(row, binding=binding)

    def latest_cursor(self, *, binding: BridgeContext) -> int:
        run = self._run_for_read(binding)
        return 0 if run is None else int(run.next_cursor)

    def all_events(self, *, binding: BridgeContext) -> tuple[StepEvent, ...]:
        return self.read_after(binding=binding, after_cursor=0)


# Descriptive aliases used by integration code and tests that call this a
# durable CSP store rather than naming the SQLAlchemy implementation directly.
DurableSessionEventStore = SqlAlchemySessionEventStore
CspSessionEventStoreAdapter = SqlAlchemySessionEventStore
SessionEventStoreAdapter = SqlAlchemySessionEventStore
# Keep a concise CSP-specific name for callers that do not import the pure
# protocol from ``stream_bridge`` (the protocol itself remains unchanged).
CspSessionEventStore = SqlAlchemySessionEventStore


__all__ = [
    "CspSessionEventStoreAdapter",
    "CspSessionEventStore",
    "DurableSessionEventStore",
    "SessionEventStoreAdapter",
    "SqlAlchemySessionEventStore",
]
