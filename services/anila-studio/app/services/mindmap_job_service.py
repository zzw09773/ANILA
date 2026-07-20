"""In-memory job manager for the Studio Mindmap pipeline.

Mirrors the design of :mod:`app.services.studio_job_service` (slide
deck pipeline) — same FIFO eviction, same lazy stale prune, same
immutable record pattern. The differences:

  * ``MindmapJobRecord`` carries different terminal artifacts: SVG bytes
    and DOT source string, not pptx bytes.
  * ``MAX_JOBS_PER_USER`` is set to 10 (the brief specifies it
    explicitly; slides uses 8). Mindmaps are much smaller files so we
    can afford a slightly bigger per-user window.
  * Same 60-minute stale prune.

If the anila-studio process restarts mid-job: the asyncio task is gone,
GETs for that job_id return 404, the frontend artifact marks itself
"failed". Same contract as slides.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from app.config import settings
from app.schemas.mindmap import MindmapJobStatus, MindmapPreset
from app.services import job_lifecycle
from app.services.job_lifecycle import ArtifactInfo, JobReportContext

logger = logging.getLogger(__name__)


# Brief specifies MAX_JOBS_PER_USER=10 for mindmaps (vs slides=8). Newer
# jobs evict the oldest first when this is exceeded — keeps the
# in-memory footprint bounded without losing the user's recent work.
MAX_JOBS_PER_USER = 10

# 60-minute stale window — same as slides. Cleanup runs lazily on each
# state mutation.
STALE_AGE_SECONDS = 60 * 60


# ── Step labels (UI-facing) ──────────────────────────────────────────────
#
# Kept as constants (not an enum) so the SPA can switch on the string
# values without mirroring a Python enum. The set is small enough that
# adding a new step is a code change, not a schema migration.


JOB_STEP_QUEUED = "queued"
JOB_STEP_RETRIEVING = "retrieving"
JOB_STEP_GENERATING = "generating"
JOB_STEP_RENDERING = "rendering"
JOB_STEP_DONE = "done"


@dataclass(frozen=True)
class MindmapJobRecord:
    """Frozen snapshot of one mindmap job's state.

    Immutable so concurrent polling never observes a torn write — every
    transition produces a new record via ``dataclasses.replace`` and
    the dict entry is swapped atomically under the lock.

    ``svg_bytes`` / ``dot_source`` live on the record so cancellation
    after the writer finishes still surfaces an evictable blob to the
    GC. ``download_urls`` is rebuilt on read in ``to_status`` — we don't
    persist the URL shape in the dataclass because the frontend's
    artifact store only needs the relative path.
    """

    job_id: str
    user_id: int
    collection_id: int
    preset: MindmapPreset
    state: str  # pending | running | done | failed | cancelled
    step: str | None
    title: str | None
    node_count: int | None
    error: str | None
    svg_bytes: bytes | None
    dot_source: str | None
    # 驗證後的 MindmapSpec 序列化 JSON — 前端互動式樹狀檢視的資料來源
    # (fmt=json 下載)。與 svg/dot 同生命週期。
    spec_json: str | None
    created_at: datetime
    updated_at: datetime
    # Slice 8b: control-plane passthrough, back-filled after artifact register.
    artifact_id: int | None = None
    classification_level: str | None = None
    task: asyncio.Task[Any] | None = field(default=None, compare=False, repr=False)

    def to_status(self) -> MindmapJobStatus:
        """Project the record into the API-visible JSON status."""
        return MindmapJobStatus(
            job_id=self.job_id,
            state=self.state,  # type: ignore[arg-type]
            step=self.step,
            title=self.title,
            preset=self.preset,
            node_count=self.node_count,
            error=self.error,
            download_urls=None,
            artifact_id=self.artifact_id,
            classification_level=self.classification_level,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


# Single process-wide registry. Module-level so all imports share it.
_jobs: dict[str, MindmapJobRecord] = {}
_lock = asyncio.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_job_id() -> str:
    # ``m_`` prefix mirrors slides' ``j_`` so logs visually disambiguate
    # which pipeline a job id came from.
    return f"m_{secrets.token_hex(16)}"


def _evict_user_overflow(user_id: int) -> None:
    """Trim a user's mindmap jobs to MAX_JOBS_PER_USER, oldest-first."""
    user_jobs = [j for j in _jobs.values() if j.user_id == user_id]
    if len(user_jobs) <= MAX_JOBS_PER_USER:
        return
    user_jobs.sort(key=lambda j: j.created_at)
    for j in user_jobs[: len(user_jobs) - MAX_JOBS_PER_USER]:
        _jobs.pop(j.job_id, None)


def _prune_stale() -> None:
    """Drop jobs older than STALE_AGE_SECONDS regardless of state."""
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
    preset: MindmapPreset,
    runner: Callable[["MindmapJobUpdater"], Awaitable[None]],
    report_ctx: JobReportContext | None = None,
) -> MindmapJobRecord:
    """Register a new mindmap job and spawn its pipeline task.

    ``report_ctx`` (Slice 8b) carries durable persistence + CSP reporting
    + trace spans; None skips that layer (the pipeline still runs).

    Returns the initial record (state="pending") so the endpoint can
    return its job_id to the SPA immediately; the real work runs on the
    spawned asyncio.Task.
    """
    async with _lock:
        _prune_stale()
        job_id = _new_job_id()
        now = _now()
        record = MindmapJobRecord(
            job_id=job_id,
            user_id=user_id,
            collection_id=collection_id,
            preset=preset,
            state="pending",
            step=JOB_STEP_QUEUED,
            title=None,
            node_count=None,
            error=None,
            svg_bytes=None,
            dot_source=None,
            spec_json=None,
            created_at=now,
            updated_at=now,
        )
        _jobs[job_id] = record
        _evict_user_overflow(user_id)

    await job_lifecycle.on_create(record, report_ctx)

    if settings.STUDIO_DURABLE_SUPERVISOR:
        return record

    updater = MindmapJobUpdater(job_id=job_id, ctx=report_ctx)

    async def _wrapped() -> None:
        try:
            await updater.set(state="running")
            await runner(updater)
        except asyncio.CancelledError:
            await updater.set(state="cancelled", step=None, error="使用者取消")
            raise
        except Exception as e:  # noqa: BLE001 — runner can raise anything
            logger.exception("Mindmap job %s failed", job_id)
            await updater.set(state="failed", step=None, error=str(e)[:500])

    task = asyncio.create_task(_wrapped(), name=f"mindmap-job-{job_id}")

    # Re-stash the record with the task handle so cancel_job can find it.
    async with _lock:
        current = _jobs.get(job_id)
        if current is not None:
            _jobs[job_id] = replace(current, task=task)

    return record


def get_job(job_id: str) -> MindmapJobRecord | None:
    """Fetch a job by id, or None if missing/evicted."""
    return _jobs.get(job_id)


def get_user_job(job_id: str, user_id: int) -> MindmapJobRecord | None:
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


# Test-only helper: reset registry between tests so state from one
# test doesn't leak into the next. Module-private to discourage
# production use. Keep last so it's easy to spot.
def _reset_for_tests() -> None:
    """Clear the job registry. INTENDED FOR TEST FIXTURES ONLY."""
    _jobs.clear()


def artifact_info(rec: MindmapJobRecord) -> ArtifactInfo:
    """Slice 8b describe hook — mindmap primary output is the SVG.

    The SVG bytes are held in memory (``svg_bytes``) AND written to
    ``{ARTIFACTS_DIR}/{job_id}.svg``; the in-memory bytes make the content
    hash cheap.
    """
    return ArtifactInfo(
        artifact_type="mindmap",
        title=rec.title,
        storage_ref=(
            f"{settings.ARTIFACTS_DIR}/{rec.job_id}.svg"
            if rec.svg_bytes is not None
            else None
        ),
        primary_bytes=rec.svg_bytes,
        original_filename=f"{rec.job_id}.svg",
        media_type="image/svg+xml",
        result_metadata={"node_count": rec.node_count},
    )


# ── Updater (passed into the runner closure) ──────────────────────────────


class MindmapJobUpdater:
    """Narrow API the pipeline runner uses to push state transitions.

    Mirrors :class:`app.services.studio_job_service.JobUpdater`. Every
    update writes a NEW ``MindmapJobRecord`` (immutability) so readers
    seeing the dict via ``get_job`` never observe a half-mutated state.
    """

    def __init__(self, job_id: str, ctx: JobReportContext | None = None) -> None:
        self._job_id = job_id
        self._ctx = ctx

    @property
    def job_id(self) -> str:
        return self._job_id

    async def set(
        self,
        *,
        state: str | None = None,
        step: str | None = None,
        title: str | None = None,
        node_count: int | None = None,
        error: str | None = None,
        svg_bytes: bytes | None = None,
        dot_source: str | None = None,
        spec_json: str | None = None,
        artifact_id: int | None = None,
        classification_level: str | None = None,
    ) -> None:
        """Patch fields on the current record. Only specified fields
        update; passing None (the default) leaves a field as-is.

        Note: as with the slides updater, there is no way to clear a
        field back to None through this API — once set, fields stick.
        That's intentional: state transitions are monotonic and
        debugging tooling depends on "saw it once → see it forever".
        """
        async with _lock:
            current = _jobs.get(self._job_id)
            if current is None:
                # Evicted while runner was still going. Drop silently.
                return
            patch: dict[str, Any] = {"updated_at": _now()}
            if state is not None:
                patch["state"] = state
            if step is not None:
                patch["step"] = step
            if title is not None:
                patch["title"] = title
            if node_count is not None:
                patch["node_count"] = node_count
            if error is not None:
                patch["error"] = error
            if svg_bytes is not None:
                patch["svg_bytes"] = svg_bytes
            if dot_source is not None:
                patch["dot_source"] = dot_source
            if spec_json is not None:
                patch["spec_json"] = spec_json
            if artifact_id is not None:
                patch["artifact_id"] = artifact_id
            if classification_level is not None:
                patch["classification_level"] = classification_level
            new_record = replace(current, **patch)
            _jobs[self._job_id] = new_record
        await job_lifecycle.on_transition(new_record, self._ctx, self)

    async def mark_done(
        self,
        *,
        title: str,
        node_count: int,
        svg_bytes: bytes,
        dot_source: str,
        spec_json: str | None = None,
    ) -> None:
        """Convenience: write the terminal "done" state in one call."""
        await self.set(
            state="done",
            step=JOB_STEP_DONE,
            title=title,
            node_count=node_count,
            svg_bytes=svg_bytes,
            dot_source=dot_source,
            spec_json=spec_json,
        )
