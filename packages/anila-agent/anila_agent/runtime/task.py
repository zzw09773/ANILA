"""Durable single-Agent/single-Task lifecycle for the official runtime.

This is deliberately a small adapter around the existing OpenAI Agents
``Runner`` and ``RunState``.  It owns the Silver lifecycle boundary (history,
idempotency, pause/resume, cancel and restart recovery), but it does not
replace the SDK's execution engine or the package's canonical ``StepEvent``
timeline.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import re
import tempfile
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, BinaryIO, Protocol, TypeVar, cast

from agents import RunHooks
from agents.memory import Session
from anila_contracts import StepEvent
from anila_contracts.events import StepKind, StepStatus

from anila_agent.observability.timeline import TimelineEmitter
from anila_agent.runtime.agent_factory import AssembledAgent
from anila_agent.runtime.run import run_once, run_once_state
from anila_agent.runtime.runstate import (
    dump_state,
    has_interruptions,
    load_state,
    state_from_result,
)

TASK_RECORD_SCHEMA_VERSION = "task-record/v1"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_TRecord = TypeVar("_TRecord", bound="TaskRecord")


class _WindowsFileLockApi(Protocol):
    LK_LOCK: int
    LK_UNLCK: int

    def locking(self, fd: int, mode: int, nbytes: int, /) -> None: ...


class _PosixFileLockApi(Protocol):
    LOCK_EX: int
    LOCK_UN: int

    def flock(self, fd: int, operation: int, /) -> None: ...


def _windows_file_lock_api() -> _WindowsFileLockApi:
    """Load the Windows-only lock API after the runtime platform check."""

    return cast(_WindowsFileLockApi, importlib.import_module("msvcrt"))


def _posix_file_lock_api() -> _PosixFileLockApi:
    """Load the POSIX-only lock API after the runtime platform check."""

    return cast(_PosixFileLockApi, importlib.import_module("fcntl"))


def _acquire_task_file_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        windows_api = _windows_file_lock_api()
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        windows_api.locking(handle.fileno(), windows_api.LK_LOCK, 1)
        return

    posix_api = _posix_file_lock_api()
    posix_api.flock(handle.fileno(), posix_api.LOCK_EX)


def _release_task_file_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        windows_api = _windows_file_lock_api()
        handle.seek(0)
        windows_api.locking(handle.fileno(), windows_api.LK_UNLCK, 1)
        return

    posix_api = _posix_file_lock_api()
    posix_api.flock(handle.fileno(), posix_api.LOCK_UN)


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class IdempotencyConflict(ValueError):
    """The same idempotency key was reused for different input."""


class TaskStoreCorruption(RuntimeError):
    """A durable task record is unreadable; fail closed before execution."""


class TaskStoreConflict(RuntimeError):
    """A stale writer attempted to replace a newer durable task snapshot.

    Task lifecycle writes are compare-and-swap operations.  In particular, a
    runner that finishes just after another request cancelled its task must
    never turn the durable terminal state back into ``completed``.
    """


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_aware_datetime(raw: str) -> datetime:
    value = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    return datetime.fromisoformat(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def request_digest(value: Any) -> str:
    """Hash the immutable request projection used by idempotency admission."""

    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass
class TaskRecord:
    """JSON-safe durable record for one task/run/invocation chain."""

    task_id: str
    run_id: str
    session_id: str
    invocation_id: str
    idempotency_key: str
    request_hash: str
    status: TaskStatus = TaskStatus.QUEUED
    history: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    state_string: str | None = None
    resume_idempotency_key: str | None = None
    resume_request_hash: str | None = None
    result: Any = None
    error: str | None = None
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)
    revision: int = 0

    @property
    def terminal(self) -> bool:
        return self.status in {
            TaskStatus.COMPLETED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
        }

    @staticmethod
    def _normalise_events(
        raw_events: list[dict[str, Any]],
        *,
        task_id: str,
        session_id: str,
        run_id: str,
        invocation_id: str,
    ) -> list[dict[str, Any]]:
        """Validate durable timeline data before it can be replayed as SSE.

        Task files are part of the service boundary after a restart.  Treating
        arbitrary JSON as an event would allow corrupt state to surface as an
        unvalidated named frame, so event shape and its correlation fields are
        checked on both write and read.
        """

        normalised: list[dict[str, Any]] = []
        for item in raw_events:
            try:
                event = StepEvent.model_validate(item)
            except Exception as exc:
                raise ValueError("TaskRecord event 不符合 StepEvent 契約") from exc
            if (
                event.task_id != task_id
                or event.session_id != session_id
                or event.run_id != run_id
                or event.invocation_id != invocation_id
            ):
                raise ValueError("TaskRecord event correlation 與 task 不一致")
            normalised.append(event.model_dump(mode="json"))
        return normalised

    def to_dict(self) -> dict[str, Any]:
        for field_name, value in (
            ("task_id", self.task_id),
            ("run_id", self.run_id),
            ("session_id", self.session_id),
            ("invocation_id", self.invocation_id),
        ):
            if not _SAFE_ID.fullmatch(value):
                raise ValueError(f"TaskRecord {field_name} identifier 無效")
        if not _SAFE_IDEMPOTENCY_KEY.fullmatch(self.idempotency_key):
            raise ValueError("TaskRecord idempotency_key 不合法")
        if not re.fullmatch(r"[0-9a-f]{64}", self.request_hash):
            raise ValueError("TaskRecord request_hash 必須是 sha256 hex")
        if (self.resume_idempotency_key is None) != (self.resume_request_hash is None):
            raise ValueError("TaskRecord resume idempotency 欄位必須成對")
        if self.resume_idempotency_key is not None:
            if not _SAFE_IDEMPOTENCY_KEY.fullmatch(self.resume_idempotency_key):
                raise ValueError("TaskRecord resume_idempotency_key 不合法")
            if self.resume_request_hash is None or not re.fullmatch(
                r"[0-9a-f]{64}", self.resume_request_hash
            ):
                raise ValueError("TaskRecord resume_request_hash 必須是 sha256 hex")
        for field_name, timestamp in (
            ("created_at", self.created_at),
            ("updated_at", self.updated_at),
        ):
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError(f"TaskRecord {field_name} 必須含 timezone")
        events = self._normalise_events(
            self.events,
            task_id=self.task_id,
            session_id=self.session_id,
            run_id=self.run_id,
            invocation_id=self.invocation_id,
        )
        return {
            "schema_version": TASK_RECORD_SCHEMA_VERSION,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "invocation_id": self.invocation_id,
            "idempotency_key": self.idempotency_key,
            "request_hash": self.request_hash,
            "status": self.status.value,
            "history": self.history,
            "events": events,
            "state_string": self.state_string,
            "resume_idempotency_key": self.resume_idempotency_key,
            "resume_request_hash": self.resume_request_hash,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "revision": self.revision,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls: type[_TRecord], value: Mapping[str, Any]) -> _TRecord:
        if value.get("schema_version") != TASK_RECORD_SCHEMA_VERSION:
            raise ValueError("不支援的 TaskRecord schema_version")
        expected_keys = {
            "schema_version",
            "task_id",
            "run_id",
            "session_id",
            "invocation_id",
            "idempotency_key",
            "request_hash",
            "status",
            "history",
            "events",
            "state_string",
            "resume_idempotency_key",
            "resume_request_hash",
            "result",
            "error",
            "created_at",
            "updated_at",
            "revision",
        }
        extra = set(value) - expected_keys
        if extra:
            raise ValueError(f"TaskRecord 含未知欄位: {sorted(extra)!r}")
        try:
            status = TaskStatus(str(value["status"]))
            created_at = _parse_aware_datetime(str(value["created_at"]))
            updated_at = _parse_aware_datetime(str(value["updated_at"]))
        except (KeyError, ValueError) as exc:
            raise ValueError("TaskRecord 欄位或 status 無效") from exc
        if (
            created_at.tzinfo is None
            or created_at.utcoffset() is None
            or updated_at.tzinfo is None
            or updated_at.utcoffset() is None
        ):
            raise ValueError("TaskRecord datetime 必須含 timezone")
        required = (
            "task_id",
            "run_id",
            "session_id",
            "invocation_id",
            "idempotency_key",
            "request_hash",
        )
        if any(not str(value.get(key, "")).strip() for key in required):
            raise ValueError("TaskRecord correlation/idempotency 欄位不得為空")
        for key in ("task_id", "run_id", "session_id", "invocation_id"):
            if not _SAFE_ID.fullmatch(str(value[key])):
                raise ValueError(f"TaskRecord {key} identifier 無效")
        if not re.fullmatch(r"[0-9a-f]{64}", str(value["request_hash"])):
            raise ValueError("TaskRecord request_hash 必須是 sha256 hex")
        if not _SAFE_IDEMPOTENCY_KEY.fullmatch(str(value["idempotency_key"])):
            raise ValueError("TaskRecord idempotency_key 不合法")
        resume_key = value.get("resume_idempotency_key")
        resume_hash = value.get("resume_request_hash")
        if (resume_key is None) != (resume_hash is None):
            raise ValueError("TaskRecord resume idempotency 欄位必須成對")
        if resume_key is not None:
            if not isinstance(resume_key, str) or not _SAFE_IDEMPOTENCY_KEY.fullmatch(resume_key):
                raise ValueError("TaskRecord resume_idempotency_key 不合法")
            if not isinstance(resume_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", resume_hash):
                raise ValueError("TaskRecord resume_request_hash 必須是 sha256 hex")
        history_raw = value.get("history", [])
        events_raw = value.get("events", [])
        if not isinstance(history_raw, list) or not isinstance(events_raw, list):
            raise ValueError("TaskRecord history/events 必須是 list")
        if any(not isinstance(item, Mapping) for item in [*history_raw, *events_raw]):
            raise ValueError("TaskRecord history/events item 必須是 object")
        error = value.get("error")
        if error is not None and (
            not isinstance(error, str) or not re.fullmatch(r"[a-z0-9_:-]+", error)
        ):
            raise ValueError("TaskRecord error 只能是安全錯誤碼")
        revision = value.get("revision", 0)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("TaskRecord revision 無效")
        events = cls._normalise_events(
            [dict(item) for item in events_raw],
            task_id=str(value["task_id"]),
            session_id=str(value["session_id"]),
            run_id=str(value["run_id"]),
            invocation_id=str(value["invocation_id"]),
        )
        return cls(
            task_id=str(value["task_id"]),
            run_id=str(value["run_id"]),
            session_id=str(value["session_id"]),
            invocation_id=str(value["invocation_id"]),
            idempotency_key=str(value["idempotency_key"]),
            request_hash=str(value["request_hash"]),
            status=status,
            history=[dict(item) for item in history_raw],
            events=events,
            state_string=value.get("state_string"),
            resume_idempotency_key=resume_key,
            resume_request_hash=resume_hash,
            result=value.get("result"),
            error=error,
            created_at=created_at,
            updated_at=updated_at,
            revision=revision,
        )

    @classmethod
    def from_json(cls: type[_TRecord], raw: str) -> _TRecord:
        value = json.loads(raw)
        if not isinstance(value, Mapping):
            raise ValueError("TaskRecord 必須是 JSON object")
        return cls.from_dict(value)


class TaskStore(Protocol):
    """Persistence protocol required by :class:`SingleTaskRunner`."""

    def create(
        self,
        *,
        task_id: str,
        run_id: str | None = None,
        session_id: str,
        invocation_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> TaskRecord: ...

    def get(self, task_id: str) -> TaskRecord: ...

    def save(self, record: TaskRecord) -> TaskRecord: ...

    def cancel(
        self, task_id: str, *, terminal_event: Mapping[str, Any] | None = None
    ) -> TaskRecord: ...

    def claim_resume(
        self, task_id: str, *, idempotency_key: str, request_hash: str
    ) -> tuple[TaskRecord, bool]: ...

    def recover(self) -> list[TaskRecord]: ...


class FileTaskStore:
    """Atomic JSON-file store suitable for one official Agent host.

    Files are replaced with ``os.replace`` after a flush/fsync, so a process
    restart observes either the previous complete record or the next complete
    record, never a partially written lifecycle snapshot.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        import threading

        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._thread_lock = threading.RLock()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Process + thread lock around idempotency read/modify/write."""

        lock_path = self.root / ".tasks.lock"
        with self._thread_lock, lock_path.open("a+b") as handle:
            _acquire_task_file_lock(handle)
            try:
                yield
            finally:
                _release_task_file_lock(handle)

    @staticmethod
    def _validate_id(value: str, *, field_name: str) -> str:
        if not _SAFE_ID.fullmatch(value):
            raise ValueError(f"{field_name} 不合法")
        return value

    @staticmethod
    def _validate_idempotency_key(value: str) -> str:
        if not _SAFE_IDEMPOTENCY_KEY.fullmatch(value):
            raise ValueError("idempotency_key 必須是 1-255 字元的安全 identifier")
        return value

    def path_for(self, task_id: str) -> Path:
        return self.root / f"{self._validate_id(task_id, field_name='task_id')}.json"

    def ensure_writable(self) -> None:
        """Prove the durable volume can create and fsync a task-side file.

        ``mkdir`` alone is insufficient when a mounted volume exists but is
        read-only.  Startup admission uses this probe before it advertises
        resume support, so a manifest can never promise durability that this
        process cannot actually provide.
        """

        with self._locked():
            fd, tmp_name = tempfile.mkstemp(prefix=".task-store-probe.", dir=self.root)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(b"anila-task-store-probe\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)

    def _read_path(self, path: Path) -> TaskRecord:
        return TaskRecord.from_json(path.read_text(encoding="utf-8"))

    def get(self, task_id: str) -> TaskRecord:
        path = self.path_for(task_id)
        if not path.is_file():
            raise FileNotFoundError(task_id)
        return self._read_path(path)

    @staticmethod
    def _same_content(left: TaskRecord, right: TaskRecord) -> bool:
        """Compare lifecycle content while deliberately ignoring write metadata.

        A retry may hold an older ``revision``/``updated_at`` pair but be
        otherwise byte-for-byte equivalent.  That is the sole stale-write case
        accepted as idempotent; every meaningful difference fails closed.
        """

        left_value = left.to_dict()
        right_value = right.to_dict()
        for key in ("revision", "updated_at"):
            left_value.pop(key)
            right_value.pop(key)
        return left_value == right_value

    @staticmethod
    def _synchronise(target: TaskRecord, source: TaskRecord) -> TaskRecord:
        """Refresh a caller-held record without changing its object identity."""

        target.task_id = source.task_id
        target.run_id = source.run_id
        target.session_id = source.session_id
        target.invocation_id = source.invocation_id
        target.idempotency_key = source.idempotency_key
        target.request_hash = source.request_hash
        target.status = source.status
        target.history = source.history
        target.events = source.events
        target.state_string = source.state_string
        target.resume_idempotency_key = source.resume_idempotency_key
        target.resume_request_hash = source.resume_request_hash
        target.result = source.result
        target.error = source.error
        target.created_at = source.created_at
        target.updated_at = source.updated_at
        target.revision = source.revision
        return target

    def _find_idempotency(self, key: str) -> TaskRecord | None:
        for path in sorted(self.root.glob("*.json")):
            try:
                record = self._read_path(path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise TaskStoreCorruption(f"無法讀取 durable task record: {path.name}") from exc
            if record.idempotency_key == key:
                return record
        return None

    def create(
        self,
        *,
        task_id: str,
        run_id: str | None = None,
        session_id: str,
        invocation_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> TaskRecord:
        with self._locked():
            self._validate_id(task_id, field_name="task_id")
            if run_id is not None:
                self._validate_id(run_id, field_name="run_id")
            self._validate_id(session_id, field_name="session_id")
            self._validate_id(invocation_id, field_name="invocation_id")
            self._validate_idempotency_key(idempotency_key)
            if not re.fullmatch(r"[0-9a-f]{64}", request_hash):
                raise ValueError("request_hash 必須是 sha256 hex")
            existing = self._find_idempotency(idempotency_key)
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise IdempotencyConflict("idempotency_key 已綁定不同 request")
                return existing
            path = self.path_for(task_id)
            if path.exists():
                try:
                    existing = self._read_path(path)
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    raise TaskStoreCorruption(f"無法讀取 durable task record: {path.name}") from exc
                if existing.request_hash != request_hash or existing.idempotency_key != idempotency_key:
                    raise IdempotencyConflict("task_id 已綁定不同 request")
                return existing
            record = TaskRecord(
                task_id=task_id,
                run_id=run_id or uuid.uuid4().hex,
                session_id=session_id,
                invocation_id=invocation_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            return self._save_unlocked(record)

    def claim_resume(
        self,
        task_id: str,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[TaskRecord, bool]:
        """Atomically claim one paused task for a CSP-authorized resume.

        The boolean is true only for the caller that owns execution.  Matching
        retries observe the existing record, so an HTTP retry cannot invoke the
        SDK a second time after restart or during an in-flight resume.
        """

        with self._locked():
            self._validate_idempotency_key(idempotency_key)
            if not re.fullmatch(r"[0-9a-f]{64}", request_hash):
                raise ValueError("resume request_hash 必須是 sha256 hex")
            record = self.get(task_id)
            if record.resume_idempotency_key is not None:
                if record.resume_idempotency_key == idempotency_key:
                    if record.resume_request_hash != request_hash:
                        raise IdempotencyConflict("resume idempotency_key 已綁定不同 request")
                    return record, False
                if record.status is not TaskStatus.PAUSED:
                    raise IdempotencyConflict("Task 已有不同 resume invocation")
            if record.status is not TaskStatus.PAUSED:
                raise ValueError("只有 paused Task 可以 resume")
            if not record.state_string:
                raise TaskStoreCorruption("paused Task 缺少 durable RunState")
            record.resume_idempotency_key = idempotency_key
            record.resume_request_hash = request_hash
            record.status = TaskStatus.RUNNING
            record.error = None
            return self._save_unlocked(record), True

    def save(self, record: TaskRecord) -> TaskRecord:
        with self._locked():
            path = self.path_for(record.task_id)
            if not path.is_file():
                raise TaskStoreConflict("durable task record disappeared before save")
            try:
                current = self._read_path(path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise TaskStoreCorruption(f"無法讀取 durable task record: {path.name}") from exc

            if current.revision != record.revision:
                if self._same_content(current, record):
                    return self._synchronise(record, current)
                raise TaskStoreConflict("durable task record revision conflict")

            # Terminal is a one-way latch.  A terminal retry is only allowed
            # when it is precisely the same durable content; no late runner may
            # alter its result, error code, event history, or terminal kind.
            if current.terminal:
                if self._same_content(current, record):
                    return self._synchronise(record, current)
                raise TaskStoreConflict("durable task terminal state is immutable")
            return self._save_unlocked(record)

    def _save_unlocked(self, record: TaskRecord) -> TaskRecord:
        path = self.path_for(record.task_id)
        record.revision += 1
        record.updated_at = _utc_now()
        fd, tmp_name = tempfile.mkstemp(prefix=f".{record.task_id}.", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(record.to_json())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        return record

    def cancel(
        self, task_id: str, *, terminal_event: Mapping[str, Any] | None = None
    ) -> TaskRecord:
        with self._locked():
            record = self.get(task_id)
            if record.terminal:
                return record
            record.status = TaskStatus.CANCELLED
            record.error = "cancelled_by_caller"
            if terminal_event is not None:
                record.events.append(dict(terminal_event))
            return self._save_unlocked(record)

    def recover(self) -> list[TaskRecord]:
        """Recover interrupted workers after process restart.

        A running task with an SDK state snapshot can safely resume; one with
        no snapshot is marked failed because replaying an unknown model/tool
        side effect would violate idempotency.
        """

        with self._locked():
            recovered: list[TaskRecord] = []
            for path in sorted(self.root.glob("*.json")):
                try:
                    record = self._read_path(path)
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    raise TaskStoreCorruption(f"無法讀取 durable task record: {path.name}") from exc
                if record.status is not TaskStatus.RUNNING:
                    continue
                if record.state_string:
                    record.status = TaskStatus.PAUSED
                    record.error = "recovered_after_restart"
                else:
                    record.status = TaskStatus.FAILED
                    record.error = "worker_restart_without_durable_state"
                recovered.append(self._save_unlocked(record))
            return recovered


class SingleTaskRunner:
    """Run exactly one governed Agent task at a time, with durable latches."""

    def __init__(self, store: TaskStore, *, gold_resume_enabled: bool = False) -> None:
        self.store = store
        self.gold_resume_enabled = gold_resume_enabled
        self._active: dict[str, asyncio.Task[TaskRecord]] = {}

    def start(self, **kwargs: Any) -> asyncio.Task[TaskRecord]:
        task = asyncio.create_task(self.run(**kwargs))
        task_id = str(kwargs["task_id"])
        self._active[task_id] = task
        task.add_done_callback(lambda _task: self._active.pop(task_id, None))
        return task

    def cancel(self, task_id: str) -> TaskRecord:
        record = self.store.cancel(task_id)
        active = self._active.get(task_id)
        if active is not None and not active.done():
            active.cancel()
        return record

    def _save_or_observe_terminal(self, record: TaskRecord) -> TaskRecord:
        """Preserve a concurrent cancel instead of leaking a stale-write error."""

        try:
            return self.store.save(record)
        except TaskStoreConflict:
            current = self.store.get(record.task_id)
            if current.terminal:
                return current
            raise

    @staticmethod
    def _history_input(user_input: str | list[Any]) -> list[dict[str, Any]]:
        if isinstance(user_input, str):
            return [{"role": "user", "content": user_input}]
        return [dict(item) for item in user_input if isinstance(item, Mapping)]

    @staticmethod
    def _capture_events(record: TaskRecord, timeline: TimelineEmitter | None) -> None:
        if timeline is None:
            return
        known = {str(item.get("event_id")) for item in record.events}
        for event in timeline.events:
            if event.event_id not in known:
                record.events.append(event.model_dump(mode="json"))

    @staticmethod
    def _emit(timeline: TimelineEmitter | None, status: StepStatus, summary: str) -> None:
        if timeline is None:
            return
        timeline.emit(
            step_id=f"agent:{timeline.agent_id}",
            kind=StepKind.AGENT,
            status=status,
            safe_input_summary="開始單一 Task 執行" if status is StepStatus.RUNNING else None,
            safe_output_summary=summary if status is not StepStatus.RUNNING else None,
            terminal=status
            in {StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.CANCELLED},
        )

    async def run(
        self,
        *,
        task_id: str,
        session_id: str,
        invocation_id: str,
        idempotency_key: str,
        user_input: str | list[Any],
        assembled: AssembledAgent,
        session: Session | None = None,
        hooks: RunHooks | None = None,
        timeline: TimelineEmitter | None = None,
    ) -> TaskRecord:
        digest = request_digest({"session_id": session_id, "input": user_input})
        record = self.store.create(
            task_id=task_id,
            run_id=timeline.run_id if timeline is not None else None,
            session_id=session_id,
            invocation_id=invocation_id,
            idempotency_key=idempotency_key,
            request_hash=digest,
        )
        if record.terminal or record.status is TaskStatus.PAUSED:
            return record
        if record.status is TaskStatus.RUNNING:
            return record

        record.status = TaskStatus.RUNNING
        if not record.history:
            record.history.extend(self._history_input(user_input))
        self._emit(timeline, StepStatus.RUNNING, "")
        self._capture_events(record, timeline)
        self._save_or_observe_terminal(record)
        try:
            result = await run_once(assembled, user_input, session=session, hooks=hooks)
            if has_interruptions(result):
                record.state_string = dump_state(state_from_result(result))
                record.status = TaskStatus.PAUSED
            else:
                record.status = TaskStatus.COMPLETED
                final_output = getattr(result, "final_output", None)
                record.result = final_output
                if final_output is not None:
                    record.history.append({"role": "assistant", "content": str(final_output)})
                try:
                    record.state_string = dump_state(state_from_result(result))
                except Exception:
                    record.state_string = None
            self._emit(
                timeline,
                StepStatus.BLOCKED if record.status is TaskStatus.PAUSED else StepStatus.COMPLETED,
                "等待核准/恢復" if record.status is TaskStatus.PAUSED else "Task 執行完成",
            )
            self._capture_events(record, timeline)
            return self._save_or_observe_terminal(record)
        except asyncio.CancelledError:
            record.status = TaskStatus.CANCELLED
            record.error = "cancelled_by_caller"
            self._emit(timeline, StepStatus.CANCELLED, "Task 已取消")
            self._capture_events(record, timeline)
            self._save_or_observe_terminal(record)
            raise
        except Exception:
            record.status = TaskStatus.FAILED
            record.error = "execution_failed"
            self._emit(timeline, StepStatus.FAILED, "Task 執行失敗")
            self._capture_events(record, timeline)
            return self._save_or_observe_terminal(record)

    async def resume(
        self,
        *,
        task_id: str,
        assembled: AssembledAgent,
        session: Session | None = None,
        hooks: RunHooks | None = None,
        timeline: TimelineEmitter | None = None,
    ) -> TaskRecord:
        if not self.gold_resume_enabled:
            raise RuntimeError("Silver runtime 未開啟 Gold durable resume conformance")
        record = self.store.get(task_id)
        if record.status is not TaskStatus.PAUSED:
            raise ValueError("只有 paused Task 可以 resume")
        if not record.state_string:
            raise ValueError("paused Task 缺少 durable RunState")
        state = await load_state(
            assembled.agent,
            record.state_string,
            context_override=assembled.context,
        )
        record.status = TaskStatus.RUNNING
        self._emit(timeline, StepStatus.RUNNING, "恢復單一 Task")
        self._capture_events(record, timeline)
        self._save_or_observe_terminal(record)
        try:
            result = await run_once_state(assembled, state, session=session, hooks=hooks)
            if has_interruptions(result):
                record.state_string = dump_state(state_from_result(result))
                record.status = TaskStatus.PAUSED
            else:
                record.status = TaskStatus.COMPLETED
                record.result = getattr(result, "final_output", None)
                if record.result is not None:
                    record.history.append({"role": "assistant", "content": str(record.result)})
                record.state_string = dump_state(state_from_result(result))
            self._emit(
                timeline,
                StepStatus.BLOCKED if record.status is TaskStatus.PAUSED else StepStatus.COMPLETED,
                "等待再次核准/恢復" if record.status is TaskStatus.PAUSED else "Task 執行完成",
            )
            self._capture_events(record, timeline)
            return self._save_or_observe_terminal(record)
        except asyncio.CancelledError:
            record.status = TaskStatus.CANCELLED
            record.error = "cancelled_by_caller"
            self._emit(timeline, StepStatus.CANCELLED, "Task 已取消")
            self._capture_events(record, timeline)
            self._save_or_observe_terminal(record)
            raise
        except Exception:
            record.status = TaskStatus.FAILED
            record.error = "resume_failed"
            self._emit(timeline, StepStatus.FAILED, "Task 恢復失敗")
            self._capture_events(record, timeline)
            return self._save_or_observe_terminal(record)


__all__ = [
    "TASK_RECORD_SCHEMA_VERSION",
    "FileTaskStore",
    "IdempotencyConflict",
    "SingleTaskRunner",
    "TaskRecord",
    "TaskStatus",
    "TaskStore",
    "TaskStoreConflict",
    "TaskStoreCorruption",
    "request_digest",
]
