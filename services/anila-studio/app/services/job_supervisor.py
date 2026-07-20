"""Lifespan supervisor that replays all five artifact pipelines from Redis."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from app.auth import CurrentUserIdentity
from app.config import settings
from app.services import job_lifecycle, job_reporting
from app.services.job_lifecycle import JobReportContext
from app.services.job_store import LeaseLost, PersistedJob, get_job_store
from app.services.runtime_context import (
    StudioRuntimeContext,
    use_runtime_context,
)


logger = logging.getLogger(__name__)


def _timestamp(raw: str | None) -> datetime:
    if raw:
        try:
            value = datetime.fromisoformat(raw)
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


async def _hydrate_updater(
    job: PersistedJob,
    ctx: JobReportContext,
) -> tuple[Any, Any]:
    """Rebuild the owning in-memory projection and return its updater/module."""
    created = _timestamp(job.created_at)
    updated = _timestamp(job.updated_at)
    view = job.status_view

    if job.artifact_type == "slides":
        from app.services import studio_job_service as module

        async with module._lock:  # noqa: SLF001 - registry owns rehydration
            if module._jobs.get(job.job_id) is None:  # noqa: SLF001
                module._jobs[job.job_id] = module.JobRecord(  # noqa: SLF001
                    job_id=job.job_id,
                    user_id=job.owner_user_id,
                    collection_id=int(job.collection_id or 0),
                    state="running",
                    step=job.stage,
                    title=view.get("title"),
                    slide_count=view.get("slide_count"),
                    defects=(),
                    qa_passes=int(view.get("qa_passes") or 0),
                    error=None,
                    pptx_bytes=None,
                    created_at=created,
                    updated_at=updated,
                    artifact_id=job.artifact_id,
                    classification_level=job.classification_level,
                )
        return module.JobUpdater(job.job_id, ctx), module

    if job.artifact_type == "report":
        from app.schemas.report import ReportPreset
        from app.services import report_job_service as module

        preset = ReportPreset(job.request_spec["preset"])
        async with module._lock:  # noqa: SLF001
            if module._jobs.get(job.job_id) is None:  # noqa: SLF001
                module._jobs[job.job_id] = module.ReportJobRecord(  # noqa: SLF001
                    job_id=job.job_id,
                    user_id=job.owner_user_id,
                    collection_id=int(job.collection_id or 0),
                    state="running",
                    step=job.stage,
                    title=view.get("title"),
                    preset=preset,
                    sections_count=view.get("sections_count"),
                    references_count=view.get("references_count"),
                    error=None,
                    download_urls=None,
                    created_at=created,
                    updated_at=updated,
                    artifact_id=job.artifact_id,
                    classification_level=job.classification_level,
                )
        return module.ReportJobUpdater(job.job_id, ctx), module

    if job.artifact_type == "mindmap":
        from app.schemas.mindmap import MindmapPreset
        from app.services import mindmap_job_service as module

        preset = MindmapPreset(job.request_spec["preset"])
        async with module._lock:  # noqa: SLF001
            if module._jobs.get(job.job_id) is None:  # noqa: SLF001
                module._jobs[job.job_id] = module.MindmapJobRecord(  # noqa: SLF001
                    job_id=job.job_id,
                    user_id=job.owner_user_id,
                    collection_id=int(job.collection_id or 0),
                    preset=preset,
                    state="running",
                    step=job.stage,
                    title=view.get("title"),
                    node_count=view.get("node_count"),
                    error=None,
                    svg_bytes=None,
                    dot_source=None,
                    spec_json=None,
                    created_at=created,
                    updated_at=updated,
                    artifact_id=job.artifact_id,
                    classification_level=job.classification_level,
                )
        return module.MindmapJobUpdater(job.job_id, ctx), module

    if job.artifact_type == "infographic":
        from app.schemas.infographic import InfographicPreset
        from app.services import infographic_job_service as module

        preset = InfographicPreset(job.request_spec["preset"])
        async with module._lock:  # noqa: SLF001
            if module._jobs.get(job.job_id) is None:  # noqa: SLF001
                module._jobs[job.job_id] = module.InfographicJobRecord(  # noqa: SLF001
                    job_id=job.job_id,
                    user_id=job.owner_user_id,
                    collection_id=int(job.collection_id or 0),
                    preset=preset,
                    state="running",
                    step=job.stage,
                    title=view.get("title"),
                    chart_count=view.get("chart_count"),
                    error=None,
                    html_path=None,
                    pdf_path=None,
                    created_at=created,
                    updated_at=updated,
                    artifact_id=job.artifact_id,
                    classification_level=job.classification_level,
                )
        return module.InfographicJobUpdater(job.job_id, ctx), module

    if job.artifact_type == "datatable":
        from app.schemas.datatable import DatatablePreset
        from app.services import datatable_job_service as module

        preset = DatatablePreset(job.request_spec["preset"])
        async with module._lock:  # noqa: SLF001
            if module._jobs.get(job.job_id) is None:  # noqa: SLF001
                module._jobs[job.job_id] = module.DatatableJobRecord(  # noqa: SLF001
                    job_id=job.job_id,
                    user_id=job.owner_user_id,
                    collection_id=int(job.collection_id or 0),
                    state="running",
                    step=job.stage,
                    title=view.get("title"),
                    preset=preset,
                    row_count=view.get("row_count"),
                    column_count=view.get("column_count"),
                    error=None,
                    artifact_paths={},
                    created_at=created,
                    updated_at=updated,
                    artifact_id=job.artifact_id,
                    classification_level=job.classification_level,
                )
        return module.DatatableJobUpdater(job.job_id, ctx), module

    raise ValueError(f"unsupported durable artifact type: {job.artifact_type}")


async def _run_registered_pipeline(job: PersistedJob, updater: Any) -> None:
    identity = CurrentUserIdentity(
        id=job.owner_user_id,
        username=job.requester or str(job.owner_user_id),
        role="user",
        token_version=0,
    )
    if job.artifact_type == "slides":
        from app.api.studio import _run_pipeline
        from app.schemas.studio import GenerateSpecRequest

        await _run_pipeline(
            identity=identity,
            bearer="",
            payload=GenerateSpecRequest.model_validate(job.request_spec),
            updater=updater,
        )
        return
    if job.artifact_type == "report":
        from app.schemas.report import GenerateReportRequest
        from app.services.report_runner import run_report_pipeline

        await run_report_pipeline(
            request=GenerateReportRequest.model_validate(job.request_spec),
            bearer="",
            updater=updater,
        )
        return
    if job.artifact_type == "mindmap":
        from app.api.mindmaps import _run_pipeline
        from app.schemas.mindmap import GenerateMindmapRequest

        await _run_pipeline(
            identity=identity,
            bearer="",
            payload=GenerateMindmapRequest.model_validate(job.request_spec),
            updater=updater,
        )
        return
    if job.artifact_type == "infographic":
        from app.api.infographics import _run_pipeline
        from app.schemas.infographic import GenerateInfographicRequest

        await _run_pipeline(
            bearer="",
            payload=GenerateInfographicRequest.model_validate(job.request_spec),
            updater=updater,
        )
        return
    if job.artifact_type == "datatable":
        from app.api.datatables import _run_pipeline
        from app.schemas.datatable import GenerateDatatableRequest

        await _run_pipeline(
            bearer="",
            payload=GenerateDatatableRequest.model_validate(job.request_spec),
            updater=updater,
        )
        return
    raise ValueError(f"no runner registered for {job.artifact_type}")


async def _drop_live_projection(module: Any, job_id: str) -> None:
    async with module._lock:  # noqa: SLF001
        module._jobs.pop(job_id, None)  # noqa: SLF001


class JobSupervisor:
    def __init__(self) -> None:
        self._tasks: list[asyncio.Task[None]] = []
        self._worker_prefix = f"{settings.APP_NAME}:{os.getpid()}"

    @property
    def running(self) -> bool:
        return bool(self._tasks) and all(not task.done() for task in self._tasks)

    async def start(self) -> None:
        if self._tasks or not settings.STUDIO_DURABLE_SUPERVISOR:
            return
        self._tasks = [
            asyncio.create_task(
                self._worker(index),
                name=f"studio-durable-worker-{index}",
            )
            for index in range(settings.STUDIO_JOB_WORKERS)
        ]

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _worker(self, index: int) -> None:
        store = get_job_store()
        while True:
            try:
                if not store.ready:
                    await asyncio.sleep(settings.STUDIO_JOB_POLL_SECONDS)
                    continue
                await store.reap_expired()
                job = await store.claim(
                    worker_id=f"{self._worker_prefix}:{index}",
                    lease_seconds=settings.STUDIO_JOB_LEASE_SECONDS,
                )
                if job is None:
                    await asyncio.sleep(settings.STUDIO_JOB_POLL_SECONDS)
                    continue
                await self._execute(job)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - worker remains self-healing
                logger.exception("durable Studio worker loop failed")
                await asyncio.sleep(settings.STUDIO_JOB_POLL_SECONDS)

    async def _execute(self, job: PersistedJob) -> None:
        if job.lease_token is None:
            raise LeaseLost(job.job_id)
        store = get_job_store()
        terminal_status = job.checkpoint.get("terminal_status")
        if terminal_status in {"failed", "cancelled"}:
            try:
                await job_reporting.report_job_patched(
                    bearer=None,
                    job_id=job.job_id,
                    status=terminal_status,
                    error=job.last_error,
                )
                local_state = job.checkpoint.get("local_terminal_state", terminal_status)
                terminal_view = dict(job.status_view)
                terminal_view["state"] = (
                    "failed" if local_state == "dead_letter" else local_state
                )
                await store.complete(
                    replace(
                        job,
                        state=local_state,
                        stage=local_state,
                        status_view=terminal_view,
                        checkpoint={},
                    ),
                    job.lease_token,
                )
            except Exception as exc:  # noqa: BLE001 - durable reconciliation
                await store.defer_terminal_report(
                    job.job_id,
                    job.lease_token,
                    error=f"terminal report: {type(exc).__name__}: {exc}",
                    retry_delay_seconds=settings.STUDIO_JOB_RETRY_SECONDS,
                )
            return
        runtime = StudioRuntimeContext.from_job(job)
        ctx = JobReportContext(
            artifact_type=job.artifact_type,
            owner_user_id=job.owner_user_id,
            bearer="",
            collection_id=job.collection_id,
            requester=job.requester,
            task_id=job.task_id,
            source_snapshot_id=job.source_snapshot_id,
            trace_id=job.trace_id,
            request_spec=job.request_spec,
            _lease_token=job.lease_token,
            _attempt_count=job.attempt_count,
            _synchronous_finalize=True,
        )
        updater, module = await _hydrate_updater(job, ctx)
        ctx.describe = module.artifact_info
        job_lifecycle._start_heartbeat(job.job_id, ctx)  # noqa: SLF001
        try:
            await job_reporting.report_job_created(
                bearer=None,
                job_id=job.job_id,
                artifact_type=job.artifact_type,
                status="running",
                requester_user_id=job.owner_user_id,
                employee_id=job.requester,
                collection_id=job.collection_id,
                task_id=job.task_id,
                source_snapshot_id=job.source_snapshot_id,
                trace_id=job.trace_id,
            )
            authority = await job_reporting.read_job_authority(job.job_id)
            authority_status = authority.get("status") if authority is not None else None
            if authority is not None and authority.get("artifact_id") is not None:
                artifact_id = authority.get("artifact_id")
                download_url = authority.get("download_url")
                if not isinstance(artifact_id, int) or not isinstance(download_url, str):
                    raise job_reporting.ArtifactReportingError(
                        "committed CSP artifact authority is incomplete"
                    )
                recovered_view = dict(job.status_view)
                recovered_view.update(
                    {
                        "state": "done",
                        "artifact_id": artifact_id,
                        "classification_level": authority.get("classification_level"),
                    }
                )
                recovered = replace(
                    job,
                    state="done",
                    stage="done",
                    status_view=recovered_view,
                    artifact_id=artifact_id,
                    download_url=download_url,
                    classification_level=authority.get("classification_level"),
                    storage_ref=authority.get("content_hash"),
                    result_metadata={
                        **job.result_metadata,
                        "csp_version": authority.get("current_version"),
                        "recovered_after_commit": True,
                    },
                )
                await job_lifecycle._stop_heartbeat(ctx)  # noqa: SLF001
                await get_job_store().complete(recovered, job.lease_token)
                ctx._durable_terminal = True
                ctx._lease_token = None
                await _drop_live_projection(module, job.job_id)
                return
            if authority_status in {"failed", "cancelled"}:
                terminal_view = dict(job.status_view)
                terminal_view["state"] = authority_status
                await job_lifecycle._stop_heartbeat(ctx)  # noqa: SLF001
                await store.complete(
                    replace(
                        job,
                        state=authority_status,
                        stage=authority_status,
                        status_view=terminal_view,
                        checkpoint={},
                    ),
                    job.lease_token,
                )
                await _drop_live_projection(module, job.job_id)
                return
            await job_reporting.report_job_lease(
                job_id=job.job_id,
                attempt=job.attempt_count,
                lease_token=job.lease_token,
            )
            await updater.set(state="running")
            with use_runtime_context(runtime):
                await _run_registered_pipeline(job, updater)
            durable = await get_job_store().get(job.job_id)
            if durable is None or durable.state != "done":
                raise RuntimeError("pipeline returned without durable artifact completion")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - bounded durable retry
            logger.exception(
                "Studio durable job %s attempt %s failed",
                job.job_id,
                job.attempt_count,
            )
            await job_lifecycle._stop_heartbeat(ctx)  # noqa: SLF001
            current = await store.get(job.job_id)
            if (
                current is not None
                and current.state == "running"
                and current.lease_token == job.lease_token
            ):
                error = f"{type(exc).__name__}: {exc}"[:1000]
                if job.attempt_count >= job.max_attempts:
                    terminal_status = current.checkpoint.get(
                        "terminal_status", "failed"
                    )
                    local_terminal_state = current.checkpoint.get(
                        "local_terminal_state",
                        "dead_letter" if terminal_status == "failed" else terminal_status,
                    )
                    pending = replace(
                        current,
                        stage="terminal_report_pending",
                        checkpoint={
                            **current.checkpoint,
                            "terminal_status": terminal_status,
                            "local_terminal_state": local_terminal_state,
                        },
                        last_error=error,
                    )
                    await store.update_claimed(pending, job.lease_token)
                    try:
                        await job_reporting.report_job_patched(
                            bearer=None,
                            job_id=job.job_id,
                            status=terminal_status,
                            error={
                                "code": "studio_retry_exhausted",
                                "message": str(exc)[:500],
                            },
                        )
                        terminal_view = dict(pending.status_view)
                        terminal_view["state"] = (
                            "failed"
                            if local_terminal_state == "dead_letter"
                            else local_terminal_state
                        )
                        await store.complete(
                            replace(
                                pending,
                                state=local_terminal_state,
                                stage=local_terminal_state,
                                status_view=terminal_view,
                                checkpoint={},
                            ),
                            job.lease_token,
                        )
                    except Exception as report_exc:  # noqa: BLE001
                        await store.defer_terminal_report(
                            job.job_id,
                            job.lease_token,
                            error=(
                                f"terminal report: {type(report_exc).__name__}: "
                                f"{report_exc}"
                            ),
                            retry_delay_seconds=settings.STUDIO_JOB_RETRY_SECONDS,
                        )
                else:
                    await store.retry_or_dead_letter(
                        job.job_id,
                        job.lease_token,
                        error=error,
                        retry_delay_seconds=settings.STUDIO_JOB_RETRY_SECONDS,
                    )
            await _drop_live_projection(module, job.job_id)
        finally:
            await job_lifecycle._stop_heartbeat(ctx)  # noqa: SLF001


_supervisor = JobSupervisor()


def get_job_supervisor() -> JobSupervisor:
    return _supervisor


__all__ = ["JobSupervisor", "get_job_supervisor"]
