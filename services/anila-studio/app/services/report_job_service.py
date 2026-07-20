"""In-memory job manager for the Report (deep report) pipeline.

## Why a separate module from studio_job_service?

- The two pipelines have different state shapes. Slide jobs carry
  ``pptx_bytes`` + ``defects`` + ``qa_passes``; report jobs carry
  ``preset`` + ``sections_count`` + ``references_count`` +
  ``download_urls``.
- Coupling them would force every future addition to one pipeline to
  consider the other. The Phase 0 plan is explicit that report is a
  parallel, independent subsystem.
- The two ``_jobs`` registries do not collide (different module globals)
  so user-level rate limits stay per-pipeline (a user can have 8 slide
  jobs *and* 10 report jobs in flight, by design).

## Concurrency model

Identical to studio_job_service:
- ``asyncio.Lock`` around the registry,
- new ``ReportJobRecord`` per state transition (immutability),
- one ``asyncio.Task`` per job, cancellable via ``cancel_job``.

If the process restarts mid-job:
- The task is gone.
- Subsequent GETs for that job_id return 404.
- The frontend artifact, which tracks state via polling, sees 404 →
  marks itself "failed" with a retry message.

This is the same contract documented in studio_job_service.py — see
that file for the longer reasoning.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from app.config import settings
from app.schemas.report import (
    JOB_STEP_DONE,
    JOB_STEP_QUEUED,
    ReportJobStatus,
    ReportPreset,
    ReportSpec,
)
from app.services import job_lifecycle
from app.services.job_lifecycle import ArtifactInfo, JobReportContext

logger = logging.getLogger(__name__)


# Cap on simultaneously stored report jobs PER USER. Reports are heavier
# than slides (HTML + PDF + DOCX = ~1-2 MB on disk per job) so we don't
# want a runaway accumulation, but the spec sets the limit at 10 — slightly
# higher than slides (8) because users tend to iterate report instructions
# more than deck content.
MAX_JOBS_PER_USER = 10

# Jobs older than this age get reaped. Cleanup runs lazily on every state
# mutation; no background sweeper task.
STALE_AGE_SECONDS = 60 * 60  # 1 hour


@dataclass(frozen=True)
class ReportJobRecord:
    """Frozen snapshot of a Report job's state.

    Same immutability contract as JobRecord in studio_job_service: every
    state transition produces a new dataclass via ``dataclasses.replace``
    so concurrent readers never observe a torn write.
    """

    job_id: str
    user_id: int
    collection_id: int
    state: str  # pending | running | done | failed | cancelled
    step: str | None
    title: str | None
    preset: ReportPreset | None
    sections_count: int | None
    references_count: int | None
    error: str | None
    download_urls: dict[str, str] | None
    created_at: datetime
    updated_at: datetime
    # Slice 8b: control-plane passthrough, back-filled after artifact register.
    artifact_id: int | None = None
    classification_level: str | None = None
    # Loose handle to the spawned task — kept so cancel_job can call
    # task.cancel() without a separate side-table. Excluded from public
    # status views.
    task: asyncio.Task[Any] | None = field(default=None, compare=False, repr=False)

    def to_status(self) -> ReportJobStatus:
        """Project the record into the API-visible ReportJobStatus."""
        return ReportJobStatus(
            job_id=self.job_id,
            state=self.state,  # type: ignore[arg-type]
            step=self.step,
            title=self.title,
            preset=self.preset,
            sections_count=self.sections_count,
            references_count=self.references_count,
            error=self.error,
            download_urls=None,
            artifact_id=self.artifact_id,
            classification_level=self.classification_level,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


# Single process-wide registry. Module-level so all imports share it.
_jobs: dict[str, ReportJobRecord] = {}
_lock = asyncio.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_job_id() -> str:
    # Prefix ``r_`` so logs/operators can distinguish from slide jobs (``j_``).
    return f"r_{secrets.token_hex(16)}"


def _evict_user_overflow(user_id: int) -> None:
    """Trim a user's jobs to MAX_JOBS_PER_USER, oldest-first."""
    user_jobs = [j for j in _jobs.values() if j.user_id == user_id]
    if len(user_jobs) <= MAX_JOBS_PER_USER:
        return
    user_jobs.sort(key=lambda j: j.created_at)
    for j in user_jobs[: len(user_jobs) - MAX_JOBS_PER_USER]:
        _jobs.pop(j.job_id, None)


def _prune_stale() -> None:
    """Drop jobs older than STALE_AGE_SECONDS regardless of user/state."""
    cutoff = _now().timestamp() - STALE_AGE_SECONDS
    stale = [jid for jid, j in _jobs.items() if j.created_at.timestamp() < cutoff]
    for jid in stale:
        _jobs.pop(jid, None)


async def create_job(
    *,
    user_id: int,
    collection_id: int,
    preset: ReportPreset,
    runner: Callable[["ReportJobUpdater"], Awaitable[None]],
    report_ctx: JobReportContext | None = None,
) -> ReportJobRecord:
    """Register a new report job and spawn its pipeline task.

    ``runner`` is the pipeline coroutine factory; it receives a
    ``ReportJobUpdater`` so it can push state transitions without
    depending on this module's private dict directly.

    ``report_ctx`` (Slice 8b) carries durable persistence + CSP reporting
    + trace spans; None skips that layer (the pipeline still runs).

    Returns the initial record (state="pending") so the endpoint can
    return its job_id immediately.
    """
    async with _lock:
        _prune_stale()
        job_id = _new_job_id()
        now = _now()
        record = ReportJobRecord(
            job_id=job_id,
            user_id=user_id,
            collection_id=collection_id,
            state="pending",
            step=JOB_STEP_QUEUED,
            title=None,
            preset=preset,
            sections_count=None,
            references_count=None,
            error=None,
            download_urls=None,
            created_at=now,
            updated_at=now,
        )
        _jobs[job_id] = record
        _evict_user_overflow(user_id)

    await job_lifecycle.on_create(record, report_ctx)

    if settings.STUDIO_DURABLE_SUPERVISOR:
        return record

    updater = ReportJobUpdater(job_id=job_id, ctx=report_ctx)

    async def _wrapped() -> None:
        try:
            await updater.set(state="running")
            await runner(updater)
        except asyncio.CancelledError:
            await updater.set(state="cancelled", step=None, error="使用者取消")
            raise
        except Exception as e:  # noqa: BLE001 — runner can raise anything
            logger.exception("Report job %s failed", job_id)
            await updater.set(state="failed", step=None, error=str(e)[:500])

    task = asyncio.create_task(_wrapped(), name=f"report-job-{job_id}")

    async with _lock:
        current = _jobs.get(job_id)
        if current is not None:
            _jobs[job_id] = replace(current, task=task)

    return record


def get_job(job_id: str) -> ReportJobRecord | None:
    """Fetch a job by id, or None if missing/evicted."""
    return _jobs.get(job_id)


def get_user_job(job_id: str, user_id: int) -> ReportJobRecord | None:
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


def artifact_info(rec: ReportJobRecord) -> ArtifactInfo:
    """Slice 8b describe hook — report primary output is the PDF on disk.

    Reports write HTML/PDF/DOCX to ``{ARTIFACTS_DIR}/{job_id}.{fmt}``; the
    PDF is the primary. Bytes live on disk (not in memory) so we skip the
    content hash (``primary_bytes=None``) — hashing a multi-MB PDF is not
    "cheap".
    """
    storage_ref = (
        f"{settings.ARTIFACTS_DIR}/{rec.job_id}.pdf"
        if rec.state == "done"
        else None
    )
    primary_path = Path(storage_ref) if storage_ref is not None else None
    primary_bytes = (
        primary_path.read_bytes()
        if primary_path is not None and primary_path.is_file()
        else None
    )
    return ArtifactInfo(
        artifact_type="report",
        title=rec.title,
        storage_ref=storage_ref,
        primary_bytes=primary_bytes,
        original_filename=f"{rec.job_id}.pdf",
        media_type="application/pdf",
        result_metadata={
            "sections_count": rec.sections_count,
            "references_count": rec.references_count,
            "download_urls": rec.download_urls,
        },
    )


# ── Updater (passed into the runner closure) ──────────────────────────────


class ReportJobUpdater:
    """Narrow API the pipeline runner uses to push state transitions.

    Mirrors studio_job_service.JobUpdater. Every update writes a NEW
    ReportJobRecord so readers via get_job never observe half-mutated
    state.
    """

    def __init__(self, job_id: str, ctx: JobReportContext | None = None) -> None:
        self._job_id = job_id
        self._ctx = ctx

    @property
    def job_id(self) -> str:
        """Public read of the job id. Useful for the runner to derive
        artifact filenames deterministically."""
        return self._job_id

    async def set(
        self,
        *,
        state: str | None = None,
        step: str | None = None,
        title: str | None = None,
        preset: ReportPreset | None = None,
        sections_count: int | None = None,
        references_count: int | None = None,
        error: str | None = None,
        download_urls: dict[str, str] | None = None,
        artifact_id: int | None = None,
        classification_level: str | None = None,
    ) -> None:
        """Patch fields on the current record. Only specified fields are
        updated; pass None (the default) to leave a field as-is.

        Once a field is set it cannot be cleared back to None via this
        method — same monotonic state-machine contract as JobUpdater.
        """
        async with _lock:
            current = _jobs.get(self._job_id)
            if current is None:
                # Job was evicted while the runner was still going.
                return
            patch: dict[str, Any] = {"updated_at": _now()}
            if state is not None:
                patch["state"] = state
            if step is not None:
                patch["step"] = step
            if title is not None:
                patch["title"] = title
            if preset is not None:
                patch["preset"] = preset
            if sections_count is not None:
                patch["sections_count"] = sections_count
            if references_count is not None:
                patch["references_count"] = references_count
            if error is not None:
                patch["error"] = error
            if download_urls is not None:
                patch["download_urls"] = download_urls
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
        spec: ReportSpec,
        download_urls: dict[str, str],
    ) -> None:
        """Convenience: write the terminal "done" state in one call."""
        await self.set(
            state="done",
            step=JOB_STEP_DONE,
            title=spec.title,
            preset=spec.preset,
            sections_count=len(spec.sections),
            references_count=len(spec.references),
            download_urls=download_urls,
        )
