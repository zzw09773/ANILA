"""In-memory job manager for the infographic-generation pipeline.

Mirrors ``studio_job_service`` but tuned for the infographic artifact
shape: outputs are stored as ``ARTIFACTS_DIR/{job_id}.html`` /
``.pdf`` on disk (not held in memory), so the JobRecord only needs to
track metadata + state. Each render is short (10-30 s), small (~200 KB
HTML + 100-500 KB PDF), and per-user.

If anila-studio restarts mid-job the asyncio.Task is gone and any
artifact file on disk would be orphaned. The download endpoint re-checks
job state before streaming; we rely on it returning 404 if no record
matches. Disk garbage collection is out of scope for the in-memory MVP
(operators rotate ARTIFACTS_DIR manually or via cron).

## Why a separate module from the slides job service

Slides decks are held entirely in memory (``pptx_bytes``) and never hit
disk. Infographic outputs live on disk under ARTIFACTS_DIR. The two
state shapes diverge enough that sharing one module would force a
nullable / dual-mode union; keeping them parallel is clearer.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from app.schemas.infographic import (
    InfographicJobStatus,
    InfographicPreset,
    JOB_STEP_DONE,
    JOB_STEP_QUEUED,
)
from app.services import job_lifecycle
from app.services.job_lifecycle import ArtifactInfo, JobReportContext

logger = logging.getLogger(__name__)


# Per-user cap — same rationale as the slides side.
MAX_JOBS_PER_USER = 8
STALE_AGE_SECONDS = 60 * 60  # 1 hour


@dataclass(frozen=True)
class InfographicJobRecord:
    """Immutable snapshot of an infographic job.

    ``html_path`` / ``pdf_path`` are populated only once the renderer
    has written them; they're absolute paths under ARTIFACTS_DIR.
    """

    job_id: str
    user_id: int
    collection_id: int
    preset: InfographicPreset
    state: str  # pending | running | done | failed | cancelled
    step: str | None
    title: str | None
    chart_count: int | None
    error: str | None
    html_path: str | None
    pdf_path: str | None
    created_at: datetime
    updated_at: datetime
    # Slice 8b: control-plane passthrough, back-filled after artifact register.
    artifact_id: str | None = None
    classification_level: str | None = None
    task: asyncio.Task[Any] | None = field(default=None, compare=False, repr=False)

    def to_status(self) -> InfographicJobStatus:
        """Project into the API-visible InfographicJobStatus.

        download_urls is populated only on terminal "done" state — the
        frontend uses its presence as a signal to enable the download
        buttons. URLs are RELATIVE so they survive proxy / domain
        changes at deploy time.
        """
        download_urls: dict[str, str] | None = None
        if self.state == "done" and self.html_path and self.pdf_path:
            download_urls = {
                "html": f"/api/infographics/jobs/{self.job_id}/download/html",
                "pdf": f"/api/infographics/jobs/{self.job_id}/download/pdf",
            }
        return InfographicJobStatus(
            job_id=self.job_id,
            state=self.state,  # type: ignore[arg-type]
            step=self.step,
            title=self.title,
            preset=self.preset,
            chart_count=self.chart_count,
            error=self.error,
            download_urls=download_urls,
            artifact_id=self.artifact_id,
            classification_level=self.classification_level,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


_jobs: dict[str, InfographicJobRecord] = {}
_lock = asyncio.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_job_id() -> str:
    # Same shape as slides side. Prefix lets log greps disambiguate
    # artifact families: "j_" = slides, "ig_" = infographic.
    return f"ig_{secrets.token_hex(16)}"


def _evict_user_overflow(user_id: int) -> None:
    user_jobs = [j for j in _jobs.values() if j.user_id == user_id]
    if len(user_jobs) <= MAX_JOBS_PER_USER:
        return
    user_jobs.sort(key=lambda j: j.created_at)
    for j in user_jobs[: len(user_jobs) - MAX_JOBS_PER_USER]:
        _jobs.pop(j.job_id, None)


def _prune_stale() -> None:
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
    preset: InfographicPreset,
    runner: Callable[["InfographicJobUpdater"], Awaitable[None]],
    report_ctx: JobReportContext | None = None,
) -> InfographicJobRecord:
    """Register a new infographic job and spawn its pipeline task.

    ``report_ctx`` (Slice 8b) carries durable persistence + CSP reporting
    + trace spans; None skips that layer (the pipeline still runs).
    """
    async with _lock:
        _prune_stale()
        job_id = _new_job_id()
        now = _now()
        record = InfographicJobRecord(
            job_id=job_id,
            user_id=user_id,
            collection_id=collection_id,
            preset=preset,
            state="pending",
            step=JOB_STEP_QUEUED,
            title=None,
            chart_count=None,
            error=None,
            html_path=None,
            pdf_path=None,
            created_at=now,
            updated_at=now,
        )
        _jobs[job_id] = record
        _evict_user_overflow(user_id)

    await job_lifecycle.on_create(record, report_ctx)

    updater = InfographicJobUpdater(job_id=job_id, ctx=report_ctx)

    async def _wrapped() -> None:
        try:
            await updater.set(state="running")
            await runner(updater)
        except asyncio.CancelledError:
            await updater.set(state="cancelled", step=None, error="使用者取消")
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("Infographic job %s failed", job_id)
            await updater.set(state="failed", step=None, error=str(e)[:500])

    task = asyncio.create_task(_wrapped(), name=f"infographic-job-{job_id}")
    async with _lock:
        current = _jobs.get(job_id)
        if current is not None:
            _jobs[job_id] = replace(current, task=task)
    return record


def get_job(job_id: str) -> InfographicJobRecord | None:
    return _jobs.get(job_id)


def get_user_job(job_id: str, user_id: int) -> InfographicJobRecord | None:
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
    """Cancel (if running) + remove the record + best-effort artifact cleanup.

    Used by DELETE endpoint. Returns True if a job was removed.
    Artifact file deletion is best-effort: if a file is already gone or
    the path was never set, we silently skip.
    """
    rec = get_user_job(job_id, user_id)
    if rec is None:
        return False
    if rec.task is not None and not rec.task.done():
        rec.task.cancel()
    async with _lock:
        _jobs.pop(job_id, None)
    # Files outside the lock — disk I/O shouldn't block the registry.
    from pathlib import Path
    for path_str in (rec.html_path, rec.pdf_path):
        if not path_str:
            continue
        try:
            Path(path_str).unlink(missing_ok=True)
        except OSError as e:
            logger.warning(
                "Failed to delete infographic artifact %s: %s", path_str, e,
            )
    return True


def artifact_info(rec: InfographicJobRecord) -> ArtifactInfo:
    """Slice 8b describe hook — infographic primary output is the PDF.

    HTML + PDF land on disk under ARTIFACTS_DIR; the PDF is the primary
    downloadable. Bytes live on disk (not memory) so the content hash is
    skipped (not "cheap").
    """
    return ArtifactInfo(
        artifact_type="infographic",
        title=rec.title,
        storage_ref=rec.pdf_path or rec.html_path,
        primary_bytes=None,
        result_metadata={"chart_count": rec.chart_count},
    )


# ── Updater ────────────────────────────────────────────────────────────────


class InfographicJobUpdater:
    """Narrow API the pipeline runner uses to push state transitions.

    Same shape as slides JobUpdater but for the infographic record's
    field set. Every update produces a NEW frozen dataclass instance
    so readers never observe a torn write.
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
        chart_count: int | None = None,
        error: str | None = None,
        html_path: str | None = None,
        pdf_path: str | None = None,
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
            if chart_count is not None:
                patch["chart_count"] = chart_count
            if error is not None:
                patch["error"] = error
            if html_path is not None:
                patch["html_path"] = html_path
            if pdf_path is not None:
                patch["pdf_path"] = pdf_path
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
        chart_count: int,
        html_path: str,
        pdf_path: str,
    ) -> None:
        """Convenience: terminal "done" state in one call."""
        await self.set(
            state="done",
            step=JOB_STEP_DONE,
            title=title,
            chart_count=chart_count,
            html_path=html_path,
            pdf_path=pdf_path,
        )
