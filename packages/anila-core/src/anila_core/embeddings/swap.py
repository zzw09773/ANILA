"""Blue-green embedding rebuild: search stays on the old model until the new one is complete."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Callable, Protocol, Sequence


OLD_VECTOR_RETENTION_DAYS = 7
_REBUILD_OPEN = frozenset({"pending", "running"})
_FAIL_LIMIT = 3


@dataclass
class ActivationState:
    active_model_id: int | None = None
    previous_model_id: int | None = None
    switched_at: datetime | None = None
    rebuild_target_model_id: int | None = None
    rebuild_status: str | None = None
    rebuild_done: int = 0
    rebuild_total: int = 0
    rebuild_errors: int = 0
    rebuild_started_at: datetime | None = None


@dataclass(frozen=True)
class WriteTarget:
    model_id: int
    name: str
    native_dim: int
    for_column: bool


@dataclass(frozen=True)
class WorkItem:
    subject: str
    subject_id: int
    text: str
    collection_id: int | None = None


class RebuildStore(Protocol):
    def load(self) -> ActivationState: ...

    def save(self, state: ActivationState) -> None: ...

    def next_batch(self, model_id: int, limit: int) -> list[WorkItem]: ...

    def write(
        self,
        model_id: int,
        items: Sequence[WorkItem],
        vectors: Sequence[Sequence[float]],
    ) -> None: ...

    def fail(self, model_id: int, items: Sequence[WorkItem]) -> None: ...

    def counts(self, model_id: int) -> tuple[int, int, int]:
        """Return ``(total, done, exhausted_errors)``."""


def plan_designation(
    state: ActivationState,
    *,
    target_model_id: int,
    keep_model_id: int | None,
    now: datetime,
) -> ActivationState:
    """Decide what search keeps using after the console picks ``target``.

    ``keep_model_id`` is the model that already produced vectors (the
    current active pointer, or the corpus model when no pointer exists).
    Search does not move until a rebuild finishes. The first designation,
    when nothing was embedded yet, becomes active immediately.
    """
    if target_model_id <= 0:
        raise ValueError("target_model_id must be positive")
    keep = state.active_model_id if state.active_model_id is not None else keep_model_id
    if keep is None or keep == target_model_id:
        return replace(
            state,
            active_model_id=target_model_id,
            rebuild_target_model_id=None,
            rebuild_status=None,
            rebuild_done=0,
            rebuild_total=0,
            rebuild_errors=0,
            rebuild_started_at=None,
        )
    return replace(
        state,
        active_model_id=keep,
        rebuild_target_model_id=target_model_id,
        rebuild_status="pending",
        rebuild_done=0,
        rebuild_errors=0,
        rebuild_started_at=now,
    )


def plan_write_targets(
    *,
    role_id: int,
    role_name: str,
    role_native: int,
    active_id: int | None,
    active_name: str | None,
    active_native: int | None,
    rebuild_status: str | None,
) -> list[WriteTarget]:
    """Models a new row must be embedded with.

    The column (what older readers still see) stays on the model search
    is using. During a rebuild that is the previous model, and the new
    role is written beside it.
    """
    role = WriteTarget(role_id, role_name, role_native, for_column=True)
    if (
        rebuild_status in _REBUILD_OPEN
        and active_id is not None
        and active_id != role_id
        and active_name
    ):
        native = active_native if active_native and active_native > 0 else role_native
        return [
            WriteTarget(active_id, active_name, native, for_column=True),
            WriteTarget(role_id, role_name, role_native, for_column=False),
        ]
    return [role]


def rollback_state(
    state: ActivationState,
    *,
    now: datetime,
    previous_vectors_exist: bool,
) -> ActivationState:
    """Cancel an in-flight rebuild, or swap search back to the previous model.

    After a finished switch, rollback only works while the previous
    model's vectors are still stored.
    """
    if state.rebuild_status in _REBUILD_OPEN and state.active_model_id is not None:
        return replace(
            state,
            rebuild_target_model_id=None,
            rebuild_status="cancelled",
        )
    if (
        state.previous_model_id is not None
        and previous_vectors_exist
        and state.previous_model_id != state.active_model_id
    ):
        return replace(
            state,
            active_model_id=state.previous_model_id,
            previous_model_id=state.active_model_id,
            switched_at=now,
            rebuild_target_model_id=None,
            rebuild_status="complete",
        )
    raise ValueError("沒有可切回的上一個嵌入模型")


def cleanup_model_id(state: ActivationState, *, now: datetime) -> int | None:
    """Model id whose vectors are due for deletion, or None.

    The clock starts at a successful switch. A rebuild in progress
    freezes cleanup so the model search might still return to is kept.
    """
    if state.previous_model_id is None or state.switched_at is None:
        return None
    if state.rebuild_status in _REBUILD_OPEN:
        return None
    if state.previous_model_id == state.active_model_id:
        return None
    if now - state.switched_at >= timedelta(days=OLD_VECTOR_RETENTION_DAYS):
        return state.previous_model_id
    return None


def eta_seconds(
    *,
    done: int,
    total: int,
    started_at: datetime | None,
    now: datetime,
) -> int | None:
    if total <= 0:
        return None
    if done >= total:
        return 0
    if started_at is None or done <= 0:
        return None
    elapsed = (now - started_at).total_seconds()
    if elapsed <= 0:
        return None
    remaining = total - done
    return int(remaining * elapsed / done)


def run_rebuild_pass(
    store: RebuildStore,
    embed: Callable[[list[str]], list[list[float]]],
    *,
    max_batches: int,
    batch_size: int,
    now: datetime,
) -> ActivationState:
    """Embed up to ``max_batches`` and switch when nothing retryable remains.

    Stopping after ``max_batches`` leaves the job pending. The next call
    continues from rows that do not yet have a vector, which is how a
    restart resumes.
    """
    if max_batches < 0 or batch_size <= 0:
        raise ValueError("batch bounds must be positive")
    state = store.load()
    target = state.rebuild_target_model_id
    if target is None or state.rebuild_status not in _REBUILD_OPEN:
        return state

    state = replace(
        state,
        rebuild_status="running",
        rebuild_started_at=state.rebuild_started_at or now,
    )
    store.save(state)

    for _ in range(max_batches):
        total, done, errors = store.counts(target)
        state = replace(
            state,
            rebuild_done=done,
            rebuild_total=total,
            rebuild_errors=errors,
        )
        batch = store.next_batch(target, batch_size)
        if not batch:
            store.save(state)
            return _finish(store, state, now)
        try:
            vectors = embed([item.text for item in batch])
        except Exception:
            store.fail(target, batch)
            total, done, errors = store.counts(target)
            state = replace(state, rebuild_done=done, rebuild_total=total, rebuild_errors=errors)
            store.save(state)
            continue
        if len(vectors) != len(batch):
            store.fail(target, batch)
            total, done, errors = store.counts(target)
            state = replace(state, rebuild_done=done, rebuild_total=total, rebuild_errors=errors)
            store.save(state)
            continue
        store.write(target, batch, vectors)
        total, done, errors = store.counts(target)
        state = replace(state, rebuild_done=done, rebuild_total=total, rebuild_errors=errors)
        store.save(state)
    return state


def _finish(store: RebuildStore, state: ActivationState, now: datetime) -> ActivationState:
    target = state.rebuild_target_model_id
    assert target is not None
    total, done, errors = store.counts(target)
    # total 只算還能嵌入的列。三次失敗的列在 total 之外，不能擋切換。
    # 一筆都沒嵌入、而且有永久失敗時，仍然不要切過去。
    if done < total or (done == 0 and errors > 0):
        finished = replace(
            state,
            rebuild_status="failed",
            rebuild_done=done,
            rebuild_total=total,
            rebuild_errors=errors,
        )
        store.save(finished)
        return finished
    previous = state.active_model_id if state.active_model_id != target else state.previous_model_id
    finished = replace(
        state,
        previous_model_id=previous,
        active_model_id=target,
        switched_at=now,
        rebuild_target_model_id=None,
        rebuild_status="complete",
        rebuild_done=done,
        rebuild_total=total,
        rebuild_errors=errors,
    )
    store.save(finished)
    return finished


@dataclass
class InMemoryCorpus:
    """Rows plus per-model vectors. Used to prove rebuild without Postgres."""

    items: list[WorkItem] = field(default_factory=list)
    vectors: dict[tuple[str, int, int], list[float]] = field(default_factory=dict)
    failures: dict[tuple[str, int, int], int] = field(default_factory=dict)
    state: ActivationState = field(default_factory=ActivationState)

    def load(self) -> ActivationState:
        return self.state

    def save(self, state: ActivationState) -> None:
        self.state = state

    def _key(self, item: WorkItem, model_id: int) -> tuple[str, int, int]:
        return (item.subject, item.subject_id, model_id)

    def _permanently_failed(self, item: WorkItem, model_id: int) -> bool:
        return self.failures.get(self._key(item, model_id), 0) >= _FAIL_LIMIT

    def _embeddable(self, item: WorkItem, model_id: int) -> bool:
        """與正式 SQL 同一套：有文字，而且這次目標還沒被判成無法嵌入。"""
        if not (item.text or "").strip():
            return False
        return not self._permanently_failed(item, model_id)

    def next_batch(self, model_id: int, limit: int) -> list[WorkItem]:
        out: list[WorkItem] = []
        for item in self.items:
            key = self._key(item, model_id)
            if key in self.vectors:
                continue
            if not self._embeddable(item, model_id):
                continue
            out.append(item)
            if len(out) >= limit:
                break
        return out

    def write(
        self,
        model_id: int,
        items: Sequence[WorkItem],
        vectors: Sequence[Sequence[float]],
    ) -> None:
        for item, vec in zip(items, vectors):
            self.vectors[(item.subject, item.subject_id, model_id)] = list(vec)

    def fail(self, model_id: int, items: Sequence[WorkItem]) -> None:
        for item in items:
            key = (item.subject, item.subject_id, model_id)
            self.failures[key] = self.failures.get(key, 0) + 1

    def counts(self, model_id: int) -> tuple[int, int, int]:
        total = 0
        done = 0
        errors = 0
        for item in self.items:
            key = self._key(item, model_id)
            if self._permanently_failed(item, model_id) and key not in self.vectors:
                errors += 1
            if not self._embeddable(item, model_id):
                continue
            total += 1
            if key in self.vectors:
                done += 1
        return total, done, errors

    def search(self, model_id: int) -> list[int]:
        """Subject ids that have a vector for ``model_id`` only."""
        return [
            subject_id
            for (subject, subject_id, mid) in self.vectors
            if mid == model_id and subject == "chunk"
        ]

    def drop_model(self, model_id: int) -> int:
        keys = [key for key in self.vectors if key[2] == model_id]
        for key in keys:
            del self.vectors[key]
        return len(keys)
