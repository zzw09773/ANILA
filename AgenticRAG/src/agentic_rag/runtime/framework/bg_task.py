"""``BgTaskRunner`` — execute ``ActionKind.BG_TASK`` Actions asynchronously.

Where Coordinator (``coordinator.py``) is for spawning LLM-driven
sub-agents, BgTaskRunner is for **non-LLM background work**:

- Long-running ingestion pipelines (parse 10k PDFs, build chunks)
- Vector-index rebuilds
- Batch inference (re-embed everything against a new model)
- Disk-heavy maintenance jobs (compaction, dedup)

The defining trait: the LLM doesn't WAIT for these. It calls a tool
that returns a handle in milliseconds; the actual work proceeds in
an asyncio.Task on the event loop. Subsequent tool calls
(``check_bg_task`` / ``cancel_bg_task``) inspect / control progress.

Output capture:

- ``MemorySink`` keeps the worker's stdout/log lines in a list,
  capped at ``max_chars``. Cheap; loses data on process restart.
- ``FileSink`` writes to a path under a per-task directory. Survives
  process restart; the LLM can later be pointed at the file path
  to inspect.

Persistence caveat: a Python coroutine itself can't be checkpointed
across process restarts. What survives a restart is the LAST
recorded handle state + any file-sink output. After restart, the
BgTaskRunner has no in-flight tasks; the LLM can read past output
files but won't be able to resume in-flight work. Truly resumable
background jobs need an external job queue (Celery / RQ / SQS), not
this module.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from agentic_rag.runtime.framework.action import (
    Action,
    ActionContext,
    ActionKind,
    ActionResult,
)
from agentic_rag.runtime.framework.exceptions import UserError

logger = logging.getLogger(__name__)


# ── State ────────────────────────────────────────────────────────────


class BgTaskState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_bg_task_id() -> str:
    return f"bg_{uuid.uuid4().hex[:12]}"


# ── Output sinks ─────────────────────────────────────────────────────


@runtime_checkable
class OutputSink(Protocol):
    """Where a BG task's progress / log lines / partial output land."""

    def write(self, chunk: str) -> None:
        ...

    def read(self, *, tail_chars: int | None = None) -> str:
        ...

    def close(self) -> None:
        ...


class MemorySink:
    """In-memory ring-buffer-ish sink. Drops oldest text when capped."""

    def __init__(self, *, max_chars: int = 64 * 1024) -> None:
        if max_chars < 1:
            raise UserError("MemorySink.max_chars must be >= 1")
        self._max = max_chars
        self._buf: list[str] = []
        self._size = 0

    def write(self, chunk: str) -> None:
        if not chunk:
            return
        self._buf.append(chunk)
        self._size += len(chunk)
        # Trim from the front if we've exceeded the cap.
        while self._size > self._max and self._buf:
            head = self._buf.pop(0)
            self._size -= len(head)

    def read(self, *, tail_chars: int | None = None) -> str:
        joined = "".join(self._buf)
        if tail_chars is None or tail_chars >= len(joined):
            return joined
        return joined[-tail_chars:]

    def close(self) -> None:
        # MemorySink has no I/O to close; method present for Protocol.
        return None


class FileSink:
    """Append-mode file sink. Output survives process restart.

    Constructed from a base directory + task id; the actual file path
    is ``base_dir/<task_id>.log``. Caller can read the file directly
    after the task finishes (the path is exposed via ``BgTaskHandle.output_path``).
    """

    def __init__(self, base_dir: str | Path, task_id: str) -> None:
        self._path = Path(base_dir) / f"{task_id}.log"
        self._handle: Any = None

    def _open(self) -> None:
        if self._handle is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self._path.open("a", encoding="utf-8")

    def write(self, chunk: str) -> None:
        if not chunk:
            return
        self._open()
        self._handle.write(chunk)
        self._handle.flush()

    def read(self, *, tail_chars: int | None = None) -> str:
        if not self._path.exists():
            return ""
        text = self._path.read_text(encoding="utf-8", errors="replace")
        if tail_chars is None or tail_chars >= len(text):
            return text
        return text[-tail_chars:]

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None

    @property
    def path(self) -> Path:
        return self._path


# ── Handle ───────────────────────────────────────────────────────────


# Type for the async writer the BG_TASK handler receives.
# Handlers call write_progress(text) to append to the sink.
WriteProgressFn = Callable[[str], None]


# Type for what a BG_TASK handler does.
# Receives ActionContext + a write_progress callable.
# Returns a final-output dict (or any JSON-serialisable value).
BgTaskHandlerFn = Callable[
    [ActionContext, WriteProgressFn], Awaitable[Any]
]


@dataclass
class BgTaskHandle:
    """Live handle to one spawned BG_TASK.

    Mutable; the asyncio task that runs the BG_TASK writes state /
    result back as it progresses. Reading fields is safe (single-write
    per field, no torn reads in CPython).

    ``progress_chars`` exposes the cumulative size of output written
    to the sink — useful as a quick "is anything happening?" signal
    without having to fetch the full output text.

    ``output_path`` is set when the sink is a ``FileSink``; ``None``
    for ``MemorySink``.
    """

    action_name: str
    task_id: str = field(default_factory=_new_bg_task_id)
    state: BgTaskState = BgTaskState.PENDING
    created_at: datetime = field(default_factory=_utc_now)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    result: Any = None
    error: Optional[BaseException] = None
    progress_chars: int = 0
    output_path: Optional[str] = None
    sink: Optional[OutputSink] = field(default=None, repr=False)
    _task: Optional["asyncio.Task[Any]"] = field(default=None, repr=False)
    _cancel_signal: Optional[asyncio.Event] = field(default=None, repr=False)

    def is_done(self) -> bool:
        return self.state in (
            BgTaskState.COMPLETED,
            BgTaskState.FAILED,
            BgTaskState.CANCELLED,
        )

    def output_tail(self, *, tail_chars: int = 4_000) -> str:
        """Return the last ``tail_chars`` of recorded output (empty if no sink)."""
        if self.sink is None:
            return ""
        return self.sink.read(tail_chars=tail_chars)

    def to_summary(self) -> dict[str, Any]:
        """Snapshot suitable for returning from ``check_bg_task`` Action.

        Hides the live future / sink references but exposes everything
        useful for an LLM or dashboard.
        """
        return {
            "task_id": self.task_id,
            "action_name": self.action_name,
            "state": self.state.value,
            "is_done": self.is_done(),
            "progress_chars": self.progress_chars,
            "output_path": self.output_path,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "error": str(self.error) if self.error else None,
        }


# ── BgTaskRunner ─────────────────────────────────────────────────────


class BgTaskRunner:
    """Owns spawn / track / cancel of BG_TASK Actions.

    Construction options:

    - ``output_dir``: base directory for ``FileSink``s. ``None`` →
      defaults to in-memory sinks.
    - ``default_sink``: ``"memory"`` | ``"file"``. Per-spawn override
      via ``spawn(..., sink_kind=...)``.
    - ``memory_sink_max_chars``: cap for in-memory sinks.

    Re-entrant: one BgTaskRunner per Runner is the typical pattern;
    Runner.bg_tasks holds one (lazily constructed if not injected).
    """

    def __init__(
        self,
        *,
        output_dir: str | Path | None = None,
        default_sink: str = "memory",
        memory_sink_max_chars: int = 64 * 1024,
    ) -> None:
        if default_sink not in ("memory", "file"):
            raise UserError(
                f"default_sink must be 'memory' or 'file', got {default_sink!r}"
            )
        if default_sink == "file" and output_dir is None:
            raise UserError(
                "default_sink='file' requires output_dir to be set"
            )
        self._output_dir = Path(output_dir) if output_dir else None
        self._default_sink = default_sink
        self._memory_max = memory_sink_max_chars
        self._handles: dict[str, BgTaskHandle] = {}

    @property
    def handles(self) -> dict[str, BgTaskHandle]:
        return dict(self._handles)

    def get(self, task_id: str) -> BgTaskHandle | None:
        return self._handles.get(task_id)

    def list_handles(
        self, *, state: BgTaskState | None = None
    ) -> list[BgTaskHandle]:
        out = list(self._handles.values())
        if state is not None:
            out = [h for h in out if h.state is state]
        return out

    def spawn(
        self,
        action: Action,
        context: ActionContext,
        *,
        sink_kind: str | None = None,
    ) -> BgTaskHandle:
        """Spawn the action's handler as a BG task. Returns a handle immediately.

        The handler MUST accept a second positional arg: ``write_progress``,
        a synchronous callable that appends text to the sink. Handlers
        not following this contract will get a TypeError captured into
        ``handle.error``.

        Two signature variants accepted:

          1. ``async def handler(ctx, write_progress) -> Any``  — typical
          2. ``async def handler(ctx) -> Any``  — legacy; write_progress
             is set on ctx.metadata["_bg_write_progress"] for compatibility
        """
        if action.kind is not ActionKind.BG_TASK:
            raise UserError(
                f"BgTaskRunner only spawns BG_TASK actions; got {action.kind.value}"
            )

        sink_kind = sink_kind or self._default_sink
        handle = BgTaskHandle(action_name=action.name)
        if sink_kind == "file":
            if self._output_dir is None:
                raise UserError(
                    "sink_kind='file' requires output_dir on the BgTaskRunner"
                )
            sink: OutputSink = FileSink(self._output_dir, handle.task_id)
            handle.output_path = str(self._output_dir / f"{handle.task_id}.log")
        else:
            sink = MemorySink(max_chars=self._memory_max)
        handle.sink = sink
        handle._cancel_signal = asyncio.Event()
        self._handles[handle.task_id] = handle

        loop = asyncio.get_event_loop()
        handle._task = loop.create_task(
            self._drive(handle, action, context),
            name=f"bg-task:{handle.task_id}",
        )
        return handle

    def cancel(self, task_id: str) -> bool:
        """Best-effort cancel via the per-task cancel_signal Event.

        Handlers that periodically check ``ctx.metadata["_bg_cancel_signal"].is_set()``
        can short-circuit gracefully. Handlers that don't bother get
        force-cancelled at the asyncio level — Task.cancel() injects
        CancelledError into the awaiting coroutine on its next yield
        point.
        """
        handle = self._handles.get(task_id)
        if handle is None or handle.is_done():
            return False
        if handle._cancel_signal is not None:
            handle._cancel_signal.set()
        if handle._task is not None and not handle._task.done():
            handle._task.cancel()
        # State transition happens in _drive's CancelledError branch.
        return True

    async def wait(self, task_id: str) -> BgTaskHandle:
        """Block until the task finishes. For tests / explicit synchronisation.

        Awaits the underlying asyncio Task — guarantees the coroutine
        has actually run its terminal branch (set state, close sink)
        before this returns.
        """
        handle = self._handles.get(task_id)
        if handle is None:
            raise UserError(f"BgTaskRunner has no task_id {task_id!r}")
        if handle._task is not None and not handle._task.done():
            try:
                await handle._task
            except BaseException:
                pass
        return handle

    # ── Internals ────────────────────────────────────────────────────

    async def _drive(
        self,
        handle: BgTaskHandle,
        action: Action,
        context: ActionContext,
    ) -> None:
        handle.state = BgTaskState.RUNNING
        handle.started_at = _utc_now()

        sink = handle.sink

        def write_progress(text: str) -> None:
            if sink is None or not text:
                return
            sink.write(text)
            handle.progress_chars += len(text)

        # Inject the progress writer + cancel signal into ctx.metadata
        # so legacy single-arg handlers can still discover them.
        bg_metadata = dict(context.metadata)
        bg_metadata["_bg_write_progress"] = write_progress
        bg_metadata["_bg_cancel_signal"] = handle._cancel_signal
        bg_ctx = ActionContext(
            run_id=context.run_id,
            agent_name=context.agent_name,
            params=context.params,
            history=context.history,
            metadata=bg_metadata,
        )

        try:
            # Try the two-arg signature first (preferred).
            try:
                result = await action.handler(bg_ctx, write_progress)  # type: ignore[call-arg]
            except TypeError as exc:
                # Caller used the single-arg shape; fall through with metadata path.
                if "positional argument" not in str(exc) and "missing" not in str(exc):
                    raise
                result = await action.handler(bg_ctx)
        except asyncio.CancelledError:
            handle.state = BgTaskState.CANCELLED
            handle.finished_at = _utc_now()
            self._close_sink(handle)
            # Re-raise so the asyncio Task's done state is "cancelled"
            # rather than "completed" — callers awaiting the Task see
            # CancelledError, matching standard asyncio semantics.
            raise
        except BaseException as exc:  # noqa: BLE001
            handle.state = BgTaskState.FAILED
            handle.error = exc
            handle.finished_at = _utc_now()
            self._close_sink(handle)
            # Don't re-raise — the asyncio Task is "done" with no
            # outer awaiter normally; logging the captured error
            # via handle.error is the surfacing path.
            return

        # Handlers may return either a raw value OR an ActionResult.
        # Normalise so callers always see the raw .result.
        if isinstance(result, ActionResult):
            handle.result = result.output if not result.is_error else None
            if result.is_error:
                handle.state = BgTaskState.FAILED
                handle.error = RuntimeError(result.error or "bg task error")
                handle.finished_at = _utc_now()
                self._close_sink(handle)
                return
        else:
            handle.result = result

        handle.state = BgTaskState.COMPLETED
        handle.finished_at = _utc_now()
        self._close_sink(handle)

    @staticmethod
    def _close_sink(handle: BgTaskHandle) -> None:
        if handle.sink is not None:
            try:
                handle.sink.close()
            except Exception:  # noqa: BLE001
                logger.exception("BgTaskRunner: sink.close failed for %s", handle.task_id)


__all__ = [
    "BgTaskHandle",
    "BgTaskHandlerFn",
    "BgTaskRunner",
    "BgTaskState",
    "FileSink",
    "MemorySink",
    "OutputSink",
    "WriteProgressFn",
]
