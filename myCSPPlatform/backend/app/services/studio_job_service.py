"""In-memory job manager for the Studio slide-generation pipeline.

## Why in-memory (and not Redis / postgres)

Studio jobs are short-lived (60-180 s) and small (a 12-slide deck is
~150-300 KB compressed pptx). The expected usage pattern is "a few decks
per user per day", not high throughput. Persisting to disk or Redis would
multiply the surface area (cleanup, multi-worker locking, eviction
policy) for very thin benefit. CSP runs as a single uvicorn process, so
an in-process dict + asyncio task is the simplest correct design.

If CSP restarts mid-job:
  * The task is gone.
  * Subsequent GETs for that job_id return 404.
  * The frontend artifact, which was added to localStorage immediately at
    job-creation time, sees the 404 → marks itself "failed" with a
    "請重試" message.

That contract is robust enough for MVP. Phase 4+ can swap this module's
public API for a disk- or DB-backed implementation without touching the
endpoint layer.

## Concurrency model

Each `create_job` spawns ONE `asyncio.Task`. Task results land in the
shared `_jobs` dict via `_update_state`. Reading via `get_job` is safe
because we never mutate a JobRecord in-place — every state transition
produces a new dataclass instance (immutability per CLAUDE.md style
guide). Per-user isolation is enforced at the endpoint layer using
`job.user_id`.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from app.schemas.studio import (
    JOB_STEP_DONE,
    JOB_STEP_QUEUED,
    JobStatus,
    SlidesSpec,
    VisualDefect,
)

logger = logging.getLogger(__name__)


# Cap on simultaneously stored jobs PER USER. Old jobs are evicted FIFO
# once this is exceeded — the goal is not eternal history (the frontend
# artifact store handles that) but only "hold onto a few in case the user
# refreshes mid-pipeline". Eviction also reclaims memory from .pptx
# blobs, which are the real cost driver here.
MAX_JOBS_PER_USER = 8

# Jobs older than this age (regardless of state) get reaped by
# `prune_stale`. Cleanup runs lazily on every state mutation, so there's
# no background task to manage.
STALE_AGE_SECONDS = 60 * 60  # 1 hour


@dataclass(frozen=True)
class JobRecord:
    """Frozen snapshot of a Studio job's state.

    Immutable so concurrent readers don't observe torn writes during
    state transitions. The manager replaces the dict entry with a new
    record on each update via `dataclasses.replace`.
    """

    job_id: str
    user_id: int
    collection_id: int
    state: str  # pending | running | done | failed | cancelled
    step: str | None
    title: str | None
    slide_count: int | None
    defects: tuple[VisualDefect, ...]
    qa_passes: int
    error: str | None
    pptx_bytes: bytes | None
    created_at: datetime
    updated_at: datetime
    # Loose handle to the spawned task — kept so `cancel_job` can call
    # task.cancel() without a separate side-table. Excluded from public
    # status views.
    task: asyncio.Task[Any] | None = field(default=None, compare=False, repr=False)

    def to_status(self) -> JobStatus:
        """Project the record into the API-visible JobStatus."""
        return JobStatus(
            job_id=self.job_id,
            state=self.state,
            step=self.step,
            title=self.title,
            slide_count=self.slide_count,
            defects=list(self.defects),
            qa_passes=self.qa_passes,
            error=self.error,
            created_at=self.created_at.isoformat(),
            updated_at=self.updated_at.isoformat(),
        )


# Single process-wide registry. Module-level so all imports share it.
_jobs: dict[str, JobRecord] = {}
_lock = asyncio.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_job_id() -> str:
    # 16 bytes of urandom → 32-char hex → low collision probability across
    # the lifetime of this process. Prefixed for log readability.
    return f"j_{secrets.token_hex(16)}"


def _evict_user_overflow(user_id: int) -> None:
    """Trim a user's jobs to MAX_JOBS_PER_USER, oldest-first."""
    user_jobs = [j for j in _jobs.values() if j.user_id == user_id]
    if len(user_jobs) <= MAX_JOBS_PER_USER:
        return
    # Drop oldest first; keep the newest MAX_JOBS_PER_USER.
    user_jobs.sort(key=lambda j: j.created_at)
    for j in user_jobs[: len(user_jobs) - MAX_JOBS_PER_USER]:
        _jobs.pop(j.job_id, None)


def _prune_stale() -> None:
    """Drop jobs older than STALE_AGE_SECONDS regardless of user/state."""
    cutoff = _now().timestamp() - STALE_AGE_SECONDS
    stale = [
        jid for jid, j in _jobs.items() if j.created_at.timestamp() < cutoff
    ]
    for jid in stale:
        _jobs.pop(jid, None)


async def create_job(
    *,
    user_id: int,
    collection_id: int,
    runner: Callable[["JobUpdater"], Awaitable[None]],
) -> JobRecord:
    """Register a new job and spawn its pipeline task.

    `runner` is the pipeline coroutine factory; it receives a `JobUpdater`
    so it can push state transitions without depending on this module's
    private dict directly. The endpoint layer wires runner = a closure
    over (db, user, payload) that calls into studio.py helpers.

    Returns the initial JobRecord (state="pending") so the endpoint can
    immediately return its job_id to the client. The real work runs on
    an asyncio.Task spawned here.
    """
    async with _lock:
        _prune_stale()
        job_id = _new_job_id()
        now = _now()
        record = JobRecord(
            job_id=job_id,
            user_id=user_id,
            collection_id=collection_id,
            state="pending",
            step=JOB_STEP_QUEUED,
            title=None,
            slide_count=None,
            defects=(),
            qa_passes=0,
            error=None,
            pptx_bytes=None,
            created_at=now,
            updated_at=now,
        )
        _jobs[job_id] = record
        _evict_user_overflow(user_id)

    updater = JobUpdater(job_id=job_id)

    async def _wrapped() -> None:
        try:
            await updater.set(state="running")
            await runner(updater)
        except asyncio.CancelledError:
            await updater.set(state="cancelled", step=None, error="使用者取消")
            raise
        except Exception as e:  # noqa: BLE001 — runner can raise anything
            logger.exception("Studio job %s failed", job_id)
            await updater.set(state="failed", step=None, error=str(e)[:500])

    task = asyncio.create_task(_wrapped(), name=f"studio-job-{job_id}")

    # Re-stash the record with the task handle so cancel_job can find it.
    async with _lock:
        current = _jobs.get(job_id)
        if current is not None:
            _jobs[job_id] = replace(current, task=task)

    return record


def get_job(job_id: str) -> JobRecord | None:
    """Fetch a job by id, or None if missing/evicted."""
    return _jobs.get(job_id)


def get_user_job(job_id: str, user_id: int) -> JobRecord | None:
    """Like get_job, but returns None for cross-user access (acts as 404)."""
    rec = _jobs.get(job_id)
    if rec is None or rec.user_id != user_id:
        return None
    return rec


async def cancel_job(job_id: str, user_id: int) -> bool:
    """Attempt to cancel an in-flight job. Returns True if cancelled."""
    rec = get_user_job(job_id, user_id)
    if rec is None or rec.state in ("done", "failed", "cancelled"):
        return False
    if rec.task is not None and not rec.task.done():
        rec.task.cancel()
    return True


# ── Updater (passed into the runner closure) ──────────────────────────────


class JobUpdater:
    """Narrow API the pipeline runner uses to push state transitions.

    Exists so the runner doesn't need to know about `_jobs` / `_lock`.
    Every update is a write of a NEW JobRecord (immutability) so readers
    seeing the dict via `get_job` never observe a half-mutated state.
    """

    def __init__(self, job_id: str) -> None:
        self._job_id = job_id

    async def set(
        self,
        *,
        state: str | None = None,
        step: str | None = None,
        title: str | None = None,
        slide_count: int | None = None,
        defects: list[VisualDefect] | None = None,
        qa_passes: int | None = None,
        error: str | None = None,
        pptx_bytes: bytes | None = None,
    ) -> None:
        """Patch fields on the current JobRecord. Only specified fields
        are updated; pass None (the default) to leave a field as-is.

        Note: there's no way to *clear* a field back to None via this
        method — that's intentional. Once a job has a title, that title
        sticks even after subsequent transitions; once defects are
        populated, they stay. The state machine is monotonic in that
        sense.
        """
        async with _lock:
            current = _jobs.get(self._job_id)
            if current is None:
                # Job got evicted while the runner was still going. Drop
                # silently — there's nothing to update.
                return
            patch: dict[str, Any] = {"updated_at": _now()}
            if state is not None:
                patch["state"] = state
            if step is not None:
                patch["step"] = step
            if title is not None:
                patch["title"] = title
            if slide_count is not None:
                patch["slide_count"] = slide_count
            if defects is not None:
                patch["defects"] = tuple(defects)
            if qa_passes is not None:
                patch["qa_passes"] = qa_passes
            if error is not None:
                patch["error"] = error
            if pptx_bytes is not None:
                patch["pptx_bytes"] = pptx_bytes
            _jobs[self._job_id] = replace(current, **patch)

    async def mark_done(
        self,
        *,
        spec: SlidesSpec,
        pptx_bytes: bytes,
        defects: list[VisualDefect],
        qa_passes: int,
    ) -> None:
        """Convenience: write the terminal "done" state in one call."""
        await self.set(
            state="done",
            step=JOB_STEP_DONE,
            title=spec.title,
            slide_count=len(spec.slides),
            defects=defects,
            qa_passes=qa_passes,
            pptx_bytes=pptx_bytes,
        )
