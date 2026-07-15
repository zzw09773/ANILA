"""SQLAlchemy-backed durable implementation of the Gate 5 event-store port.

``StreamBridge`` owns wire validation and rebinding; this adapter owns the
durable cursor, append-only event rows and idempotency/terminal latches.  The
terminal latch is driven only by the CSP-internal ``authoritative_terminal``
bit, never by an Agent status alone.  Each append runs in one database
transaction and commits the event before returning so a fresh CSP session (or
a restarted process) can replay the same run.
"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from anila_contracts import StepEvent
from anila_contracts.events import StepStatus

from app.models.session_event import SessionEvent, SessionEventRun
from app.models.resume_authority import ResumeAttempt
from app.services.proxy.stream_bridge import (
    BridgeContext,
    DispatchClaimLostError,
    EventBindingError,
    EventBudgetExceeded,
    EventConflictError,
    EventOrderError,
    MAX_EVENTS_PER_RUN,
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

    def __init__(
        self, db: Session, *, max_events_per_run: int = MAX_EVENTS_PER_RUN
    ) -> None:
        self.db = db
        self._dispatch_claim_generation: int | None = None
        self._dispatch_claim_token_sha256: str | None = None
        self._dispatch_claim_lease_seconds: float = 900.0
        self._resume_claim_generation: int | None = None
        self._resume_claim_token_sha256: str | None = None
        self._resume_claim_cursor: int | None = None
        self._resume_claim_lease_seconds: float = 180.0
        if (
            isinstance(max_events_per_run, bool)
            or not isinstance(max_events_per_run, int)
            or max_events_per_run < 0
        ):
            raise ValueError("max_events_per_run 必須是非負整數")
        self.max_events_per_run = max_events_per_run

    @staticmethod
    def _lease_now() -> datetime:
        return _utcnow()

    @staticmethod
    def _aware(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _set_dispatch_fence(
        self, *, generation: int, token_hash: str, lease_seconds: float
    ) -> None:
        self._dispatch_claim_generation = generation
        self._dispatch_claim_token_sha256 = token_hash
        self._dispatch_claim_lease_seconds = lease_seconds

    def set_resume_fence(
        self,
        *,
        blocked_cursor: int,
        generation: int,
        token_hash: str,
        lease_seconds: float = 180.0,
    ) -> None:
        if (
            isinstance(blocked_cursor, bool)
            or not isinstance(blocked_cursor, int)
            or blocked_cursor <= 0
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation <= 0
            or not isinstance(token_hash, str)
            or len(token_hash) != 64
            or not math.isfinite(float(lease_seconds))
            or float(lease_seconds) <= 0
            or float(lease_seconds) > 900
        ):
            raise ValueError("writer fence 無效")
        self._resume_claim_cursor = blocked_cursor
        self._resume_claim_generation = generation
        self._resume_claim_token_sha256 = token_hash
        self._resume_claim_lease_seconds = float(lease_seconds)

    def clear_resume_fence(self) -> None:
        self._resume_claim_cursor = None
        self._resume_claim_generation = None
        self._resume_claim_token_sha256 = None

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

    def claim_dispatch(
        self,
        *,
        binding: BridgeContext,
        idempotency_key: str | None = None,
        lease_seconds: float = 900.0,
    ) -> bool:
        """Durably claim/reclaim an initial invocation before Agent I/O.

        The lease is committed before network I/O, so another CSP process
        cannot race the same invocation.  Once expired, a new worker may
        reclaim the row with a higher generation; every append from the old
        worker is fenced before it can mutate the event ledger.
        """

        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not math.isfinite(float(lease_seconds))
            or float(lease_seconds) <= 0
            or float(lease_seconds) > 900
        ):
            raise ValueError("dispatch lease_seconds 超出允許範圍")
        key = idempotency_key or binding.invocation_id or binding.run_id
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
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
        if (
            run.dispatch_idempotency_key_sha256 is not None
            and run.dispatch_idempotency_key_sha256 != key_hash
        ):
            raise EventConflictError(
                "相同 run 的 initial dispatch key 不一致",
                run_id=binding.run_id,
            )
        if run.terminal_event_id is not None:
            self.db.commit()
            return False
        now = self._lease_now()
        expiry = self._aware(run.dispatch_lease_expires_at)
        if (
            run.dispatch_lease_token_sha256
            and expiry is not None
            and expiry > now
        ):
            self.db.commit()
            return False
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        generation = int(run.dispatch_lease_generation or 0) + 1
        run.dispatch_idempotency_key_sha256 = key_hash
        run.dispatch_lease_token_sha256 = token_hash
        run.dispatch_lease_generation = generation
        run.dispatch_lease_expires_at = now + timedelta(seconds=float(lease_seconds))
        run.updated_at = now
        self._set_dispatch_fence(
            generation=generation,
            token_hash=token_hash,
            lease_seconds=float(lease_seconds),
        )
        self.db.flush()
        self.db.commit()
        return True

    def _assert_dispatch_fence(
        self, run: SessionEventRun, *, binding: BridgeContext
    ) -> None:
        if self._dispatch_claim_generation is None:
            return
        now = self._lease_now()
        expiry = self._aware(run.dispatch_lease_expires_at)
        if (
            int(run.dispatch_lease_generation or 0) != self._dispatch_claim_generation
            or run.dispatch_lease_token_sha256 != self._dispatch_claim_token_sha256
            or expiry is None
            or expiry <= now
        ):
            raise DispatchClaimLostError(
                "initial dispatch lease 已失效",
                run_id=binding.run_id,
            )
        run.dispatch_lease_expires_at = now + timedelta(
            seconds=self._dispatch_claim_lease_seconds
        )
        run.updated_at = now

    def _assert_resume_fence(self, *, binding: BridgeContext) -> None:
        """Lock and renew the current ResumeAttempt writer lease.

        This is intentionally checked in the same transaction as the event
        append.  A stale resume worker therefore cannot append ordinary or
        terminal events after another CSP reclaimed the attempt generation.
        """

        if self._resume_claim_generation is None:
            return
        if self._resume_claim_cursor is None or self._resume_claim_token_sha256 is None:
            raise DispatchClaimLostError("resume writer fence 缺失", run_id=binding.run_id)
        now = self._lease_now()
        attempt = (
            self.db.query(ResumeAttempt)
            .filter(
                ResumeAttempt.run_id == binding.run_id,
                ResumeAttempt.blocked_cursor == self._resume_claim_cursor,
                ResumeAttempt.status == "claimed",
                ResumeAttempt.lease_generation == self._resume_claim_generation,
                ResumeAttempt.lease_token_sha256 == self._resume_claim_token_sha256,
            )
            .populate_existing()
            .with_for_update()
            .one_or_none()
        )
        expiry = self._aware(None if attempt is None else attempt.lease_expires_at)
        if attempt is None or expiry is None or expiry <= now:
            raise DispatchClaimLostError(
                "resume writer lease 已失效",
                run_id=binding.run_id,
            )
        attempt.lease_expires_at = now + timedelta(
            seconds=self._resume_claim_lease_seconds
        )
        attempt.updated_at = now

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
        self,
        event: StepEvent,
        *,
        binding: BridgeContext,
        authoritative_terminal: bool = False,
    ) -> SessionAppendResult:
        if not isinstance(event, StepEvent):
            raise TypeError("event 必須是 StepEvent")
        if authoritative_terminal and event.status not in {
            StepStatus.COMPLETED,
            StepStatus.FAILED,
            StepStatus.CANCELLED,
        }:
            raise ValueError(
                "authoritative terminal status 必須是 completed/failed/cancelled"
            )
        self._assert_event_binding(event, binding)
        digest = self._event_digest(event)
        try:
            run = self._get_or_create_run(binding)
            self._assert_dispatch_fence(run, binding=binding)
            self._assert_resume_fence(binding=binding)
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
                if bool(existing.is_terminal) != authoritative_terminal:
                    raise EventConflictError(
                        "相同 (run_id,event_id) 的 terminal authority 不一致",
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

            # ``next_cursor`` is durable and protected by the run row lock,
            # so a fresh CSP process cannot reset this ordinary-event budget.
            # CSP authoritative terminal writes intentionally bypass the cap
            # so a run can always close after exhausting its stream budget.
            if not authoritative_terminal and int(run.next_cursor or 0) >= self.max_events_per_run:
                raise EventBudgetExceeded(
                    "run 已達 ordinary event budget",
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
            is_terminal = authoritative_terminal
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
