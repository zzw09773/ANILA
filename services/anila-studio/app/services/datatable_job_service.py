"""In-memory job manager for the Studio datatable pipeline.

Same shape + reasoning as ``studio_job_service`` (slides) — kept in a
separate module so terminal states / fields / step labels don't have to
share with the slide-deck pipeline.

## State shape

DatatableJobRecord carries:
  - spec metadata (title / preset / row_count / column_count)
  - paths to the 3 exported artifact files on disk
  - terminal error string when state="failed"

Artifact bytes live on disk (under settings.ARTIFACTS_DIR), not in memory,
because XLSX especially can be megabytes for a 200-row table. Cleanup
happens via STALE_AGE_SECONDS eviction — the orphan files get unlinked
when the record gets dropped from `_jobs`.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.schemas.datatable import (
    JOB_STEP_DONE,
    JOB_STEP_QUEUED,
    DatatableJobStatus,
    DatatablePreset,
)
from app.services import job_lifecycle
from app.services.job_lifecycle import ArtifactInfo, JobReportContext

logger = logging.getLogger(__name__)


# Same cap as slides. Each datatable job is small (a few KB to a few MB)
# so memory isn't the bottleneck — this is mainly to keep the user's UI
# history list short.
MAX_JOBS_PER_USER = 8
STALE_AGE_SECONDS = 60 * 60  # 1 hour


@dataclass(frozen=True)
class DatatableJobRecord:
    """Frozen snapshot of a datatable job's state.

    `artifact_paths` is a dict {format → Path} populated only when state
    transitions to "done". Format keys are "html", "csv", "xlsx" —
    matching the download endpoint's `fmt` path param.
    """

    job_id: str
    user_id: int
    collection_id: int
    state: str  # pending | running | done | failed | cancelled
    step: str | None
    title: str | None
    preset: DatatablePreset | None
    row_count: int | None
    column_count: int | None
    error: str | None
    artifact_paths: dict[str, Path]
    created_at: datetime
    updated_at: datetime
    # Slice 8b: control-plane passthrough, back-filled after artifact register.
    artifact_id: str | None = None
    classification_level: str | None = None
    task: asyncio.Task[Any] | None = field(default=None, compare=False, repr=False)

    def to_status(self) -> DatatableJobStatus:
        """Project the record into the API-visible DatatableJobStatus.

        download_urls is built only when artifacts exist (state="done");
        the URL shape matches the endpoint's path layout. Frontend uses
        these directly so it doesn't have to mirror the URL pattern.
        """
        download_urls: dict[str, str] | None = None
        if self.artifact_paths:
            download_urls = {
                fmt: f"/api/datatables/jobs/{self.job_id}/download/{fmt}"
                for fmt in self.artifact_paths
            }

        return DatatableJobStatus(
            job_id=self.job_id,
            state=self.state,  # type: ignore[arg-type]
            step=self.step,
            title=self.title,
            preset=self.preset,
            row_count=self.row_count,
            column_count=self.column_count,
            error=self.error,
            download_urls=download_urls,
            artifact_id=self.artifact_id,
            classification_level=self.classification_level,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


# Single process-wide registry.
_jobs: dict[str, DatatableJobRecord] = {}
_lock = asyncio.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_job_id() -> str:
    return f"dt_{secrets.token_hex(16)}"


def _unlink_artifacts(rec: DatatableJobRecord) -> None:
    """Best-effort delete of a job's on-disk artifacts.

    Errors are logged but never raised — eviction must succeed even if the
    file is already gone or permissions are weird.
    """
    for fmt, path in rec.artifact_paths.items():
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            logger.warning(
                "Datatable job %s: failed to unlink %s artifact at %s: %s",
                rec.job_id, fmt, path, exc,
            )


def _evict_user_overflow(user_id: int) -> None:
    """Trim a user's jobs to MAX_JOBS_PER_USER, oldest-first, deleting
    artifacts of the evicted records along the way.
    """
    user_jobs = [j for j in _jobs.values() if j.user_id == user_id]
    if len(user_jobs) <= MAX_JOBS_PER_USER:
        return
    user_jobs.sort(key=lambda j: j.created_at)
    for j in user_jobs[: len(user_jobs) - MAX_JOBS_PER_USER]:
        evicted = _jobs.pop(j.job_id, None)
        if evicted is not None:
            _unlink_artifacts(evicted)


def _prune_stale() -> None:
    """Drop jobs older than STALE_AGE_SECONDS + unlink artifacts."""
    cutoff = _now().timestamp() - STALE_AGE_SECONDS
    stale = [
        jid for jid, j in _jobs.items() if j.created_at.timestamp() < cutoff
    ]
    for jid in stale:
        evicted = _jobs.pop(jid, None)
        if evicted is not None:
            _unlink_artifacts(evicted)


async def create_job(
    *,
    user_id: int,
    collection_id: int,
    runner: Callable[["DatatableJobUpdater"], Awaitable[None]],
    report_ctx: JobReportContext | None = None,
) -> DatatableJobRecord:
    """Register a new datatable job and spawn its pipeline task.

    `runner` is a coroutine factory taking a `DatatableJobUpdater`. Same
    contract as slides' job manager — the runner pushes state transitions
    through the updater, never touches this module's internals directly.

    ``report_ctx`` (Slice 8b) carries durable persistence + CSP reporting
    + trace spans; None skips that layer (the pipeline still runs).
    """
    async with _lock:
        _prune_stale()
        job_id = _new_job_id()
        now = _now()
        record = DatatableJobRecord(
            job_id=job_id,
            user_id=user_id,
            collection_id=collection_id,
            state="pending",
            step=JOB_STEP_QUEUED,
            title=None,
            preset=None,
            row_count=None,
            column_count=None,
            error=None,
            artifact_paths={},
            created_at=now,
            updated_at=now,
        )
        _jobs[job_id] = record
        _evict_user_overflow(user_id)

    await job_lifecycle.on_create(record, report_ctx)

    updater = DatatableJobUpdater(job_id=job_id, ctx=report_ctx)

    async def _wrapped() -> None:
        try:
            await updater.set(state="running")
            await runner(updater)
        except asyncio.CancelledError:
            await updater.set(state="cancelled", step=None, error="使用者取消")
            raise
        except Exception as e:  # noqa: BLE001 — runner can raise anything
            logger.exception("Datatable job %s failed", job_id)
            await updater.set(state="failed", step=None, error=str(e)[:500])

    task = asyncio.create_task(_wrapped(), name=f"datatable-job-{job_id}")

    async with _lock:
        current = _jobs.get(job_id)
        if current is not None:
            _jobs[job_id] = replace(current, task=task)

    return record


def get_job(job_id: str) -> DatatableJobRecord | None:
    return _jobs.get(job_id)


def get_user_job(job_id: str, user_id: int) -> DatatableJobRecord | None:
    """Like get_job but enforces per-user isolation (404 surface)."""
    rec = _jobs.get(job_id)
    if rec is None or rec.user_id != user_id:
        return None
    return rec


async def cancel_job(job_id: str, user_id: int) -> bool:
    rec = get_user_job(job_id, user_id)
    if rec is None or rec.state in ("done", "failed", "cancelled"):
        return False
    if rec.task is not None and not rec.task.done():
        rec.task.cancel()
    return True


async def delete_job(job_id: str, user_id: int) -> bool:
    """Hard-delete a terminal job: drops the record AND removes artifact
    files. Returns False when the job doesn't exist or isn't owned by
    the caller; running jobs are first cancelled then dropped.
    """
    async with _lock:
        rec = _jobs.get(job_id)
        if rec is None or rec.user_id != user_id:
            return False
        if rec.task is not None and not rec.task.done():
            rec.task.cancel()
        evicted = _jobs.pop(job_id, None)
    if evicted is not None:
        _unlink_artifacts(evicted)
    return True


def artifact_info(rec: DatatableJobRecord) -> ArtifactInfo:
    """Slice 8b describe hook — datatable primary output is the XLSX.

    Three formats (HTML/CSV/XLSX) land on disk under ARTIFACTS_DIR; XLSX is
    the primary downloadable (HTML fallback). Bytes are on disk so the
    content hash is skipped.
    """
    primary = rec.artifact_paths.get("xlsx") or rec.artifact_paths.get("html")
    return ArtifactInfo(
        artifact_type="datatable",
        title=rec.title,
        storage_ref=(str(primary) if primary is not None else None),
        primary_bytes=None,
        result_metadata={
            "row_count": rec.row_count,
            "column_count": rec.column_count,
        },
    )


# ── Updater (passed into the runner closure) ──────────────────────────────


class DatatableJobUpdater:
    """Narrow API for the pipeline runner to push state transitions.

    Mirrors slides' JobUpdater shape but with datatable-specific fields:
    `preset`, `row_count`, `column_count`, `artifact_paths`.
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
        preset: DatatablePreset | None = None,
        row_count: int | None = None,
        column_count: int | None = None,
        error: str | None = None,
        artifact_paths: dict[str, Path] | None = None,
        artifact_id: str | None = None,
        classification_level: str | None = None,
    ) -> None:
        async with _lock:
            current = _jobs.get(self._job_id)
            if current is None:
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
            if row_count is not None:
                patch["row_count"] = row_count
            if column_count is not None:
                patch["column_count"] = column_count
            if error is not None:
                patch["error"] = error
            if artifact_paths is not None:
                patch["artifact_paths"] = dict(artifact_paths)
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
        preset: DatatablePreset,
        row_count: int,
        column_count: int,
        artifact_paths: dict[str, Path],
    ) -> None:
        """Convenience: write the terminal "done" state in one call."""
        await self.set(
            state="done",
            step=JOB_STEP_DONE,
            title=title,
            preset=preset,
            row_count=row_count,
            column_count=column_count,
            artifact_paths=artifact_paths,
        )
