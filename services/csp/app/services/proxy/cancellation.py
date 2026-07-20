"""Process-local cancellation control for Gate 4 live streams.

Durability/replay across process restarts is intentionally out of scope until
Gate 5.  Within one CSP worker, a Task id maps to the currently active stream's
``asyncio.Event`` so the authenticated cancel API can stop downstream I/O.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum


class StreamCancelled(Exception):
    pass


class CancellationDisposition(str, Enum):
    """Outcome of an authenticated cancel request for one live Task."""

    ACCEPTED = "accepted"
    IN_PROGRESS = "in_progress"
    NO_ACTIVE_STREAM = "no_active_stream"


@dataclass(frozen=True, slots=True)
class CancellationResult:
    """Explicit cancellation outcome; avoids treating duplicate requests as failure."""

    disposition: CancellationDisposition

    @property
    def accepted(self) -> bool:
        return self.disposition in {
            CancellationDisposition.ACCEPTED,
            CancellationDisposition.IN_PROGRESS,
        }

    @property
    def in_progress(self) -> bool:
        return self.disposition is CancellationDisposition.IN_PROGRESS


class InSessionCancellationRegistry:
    def __init__(self) -> None:
        self._events: dict[int, set[asyncio.Event]] = {}
        # A cancel request is accepted once per live Task.  A Task can have
        # more than one consumer while the response is being torn down; the
        # terminal frame still must be claimed by exactly one consumer.
        self._cancel_requested: set[int] = set()
        self._terminal_claimed: set[int] = set()
        # The first registration is the browser-facing/root stream. Nested
        # Router -> CSP streams may observe the same cancellation, but cannot
        # steal terminal ownership from this lease.
        self._terminal_owner: dict[int, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def register(self, task_id: int | None):
        if task_id is None:
            yield None
            return
        event = asyncio.Event()
        async with self._lock:
            if task_id not in self._events:
                self._terminal_owner.setdefault(task_id, event)
            self._events.setdefault(task_id, set()).add(event)
            if task_id in self._cancel_requested:
                event.set()
        try:
            yield event
        finally:
            async with self._lock:
                events = self._events.get(task_id)
                if events is not None:
                    events.discard(event)
                    if not events:
                        self._events.pop(task_id, None)
                        if task_id not in self._cancel_requested:
                            self._terminal_owner.pop(task_id, None)
                            self._terminal_claimed.discard(task_id)

    async def cancel(self, task_id: int) -> CancellationResult:
        async with self._lock:
            events = tuple(self._events.get(task_id, ()))
            if task_id in self._cancel_requested:
                # Idempotent retry while the stream is unwinding.  The caller
                # must keep its socket open so the trusted terminal can arrive.
                return CancellationResult(CancellationDisposition.IN_PROGRESS)
            if not events:
                return CancellationResult(CancellationDisposition.NO_ACTIVE_STREAM)
            self._cancel_requested.add(task_id)
            for event in events:
                event.set()
            return CancellationResult(CancellationDisposition.ACCEPTED)

    async def claim_cancel_terminal(self, task_id: int, event: asyncio.Event) -> bool:
        """Atomically reserve the one CSP-authored cancelled terminal frame."""
        async with self._lock:
            if task_id not in self._cancel_requested:
                return False
            owner = self._terminal_owner.get(task_id)
            if owner is not event:
                return False
            if task_id in self._terminal_claimed:
                return False
            self._terminal_claimed.add(task_id)
            return True

    async def finish(self, task_id: int, event: asyncio.Event) -> tuple[bool, bool]:
        """Close a stream registration and claim a cancellation race.

        Removing the event under the same lock used by ``cancel`` closes the
        small window where the upstream has completed but the context manager
        has not yet exited.  A cancel arriving after this method returns sees
        no live stream and is correctly rejected.
        """
        async with self._lock:
            events = self._events.get(task_id)
            if events is None:
                return False, False
            events.discard(event)
            was_cancelled = event.is_set()
            terminal_claimed = (
                was_cancelled
                and task_id in self._cancel_requested
                and task_id not in self._terminal_claimed
                and self._terminal_owner.get(task_id) is event
            )
            if terminal_claimed:
                self._terminal_claimed.add(task_id)
            if not events:
                self._events.pop(task_id, None)
                if task_id not in self._cancel_requested:
                    self._terminal_owner.pop(task_id, None)
                    self._terminal_claimed.discard(task_id)
            return was_cancelled, terminal_claimed

    async def complete(self, task_id: int) -> bool:
        """Release cancellation state after the durable terminal is committed.

        A stream may have stopped yielding while its Task closure is still
        being persisted.  Keeping the request in ``IN_PROGRESS`` during that
        window prevents a duplicate browser click from triggering local abort.
        """
        async with self._lock:
            if self._events.get(task_id):
                return False
            existed = (
                task_id in self._cancel_requested
                or task_id in self._terminal_claimed
                or task_id in self._terminal_owner
            )
            self._cancel_requested.discard(task_id)
            self._terminal_claimed.discard(task_id)
            self._terminal_owner.pop(task_id, None)
            return existed


registry = InSessionCancellationRegistry()


async def cancellable_iter(iterator, cancel_event: asyncio.Event | None):
    """Yield an async iterator while racing each read against cancellation."""
    if cancel_event is None:
        async for item in iterator:
            yield item
        return
    source = iterator.__aiter__()
    try:
        while True:
            next_item = asyncio.create_task(anext(source))
            cancelled = asyncio.create_task(cancel_event.wait())
            done, pending = await asyncio.wait(
                {next_item, cancelled}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if cancelled in done and cancelled.result():
                next_item.cancel()
                await asyncio.gather(next_item, return_exceptions=True)
                raise StreamCancelled("live task cancellation requested")
            try:
                yield next_item.result()
            except StopAsyncIteration:
                return
    finally:
        close = getattr(source, "aclose", None)
        if close is not None:
            await close()


__all__ = [
    "CancellationDisposition",
    "CancellationResult",
    "InSessionCancellationRegistry",
    "StreamCancelled",
    "cancellable_iter",
    "registry",
]
