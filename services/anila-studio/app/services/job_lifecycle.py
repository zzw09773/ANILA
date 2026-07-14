"""Cross-cutting job lifecycle coordinator for the studio pipelines (Slice 8b).

Every one of the five artifact pipelines drives its state through a
``*JobUpdater``. That updater is the single choke point for state
transitions, so it is where the three cross-cutting concerns hang:

1. **Durable persistence** — write a :class:`~app.services.job_store.PersistedJob`
   projection to Redis on *every* transition, so a restarted studio can
   answer status queries for pre-restart jobs (doc 02 failure model).
2. **CSP reporting** — POST /v1/artifact-jobs on create, PATCH on a
   terminal state, POST /v1/artifacts when an artifact lands (with
   task_id / source_snapshot_id passthrough + storage_ref + content_hash).
3. **Trace spans** — a root ``studio.job`` span + a ``studio.stage`` span
   per pipeline step, shipped to CSP when a trace_id is present.

Trace emission remains fire-and-forget. Artifact control-plane calls are
detached from generation latency, but failures degrade readiness and a failed
terminal registration changes the local job to ``failed``. Persistence (1) is
best-effort too (``put_quietly``). The endpoints build a
:class:`JobReportContext`, hand it to ``create_job``, and the updater
calls into here.

Does NOT import ``anila_core``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol

from app.config import is_formal_profile, settings
from app.services import job_reporting
from app.services.job_store import (
    JobStoreUnavailable,
    LeaseLost,
    PersistedJob,
    get_job_store,
)
from app.services.studio_trace import StudioTraceEmitter


logger = logging.getLogger(__name__)


# Detached background tasks for the CSP reporting + span flush. These must
# NOT run inside the pipeline's asyncio task: their retry sleeps would keep
# the task alive after the job is logically done, and a cancellation during
# that window would flip the job to "cancelled". Running them detached keeps
# reporting fully decoupled from generation. We hold strong references so
# the tasks aren't GC'd mid-flight; ``drain()`` lets tests flush them.
_background_tasks: set[asyncio.Task[Any]] = set()
_heartbeat_tasks: set[asyncio.Task[Any]] = set()


class DurableBindingError(ValueError):
    """A formal artifact request lacks canonical Task/Snapshot/Trace scope."""
_WORKER_ID = f"{settings.APP_NAME}:{os.getpid()}"


def _spawn(coro: Awaitable[None]) -> asyncio.Task[None]:
    task = asyncio.ensure_future(coro)
    _background_tasks.add(task)

    def _finished(done: asyncio.Task[Any]) -> None:
        _background_tasks.discard(done)
        if done.cancelled():
            return
        exc = done.exception()
        if exc is not None:
            logger.error("detached artifact control-plane task failed: %s", exc)

    task.add_done_callback(_finished)
    return task


async def drain() -> None:
    """Await all in-flight reporting tasks on the current loop. Test helper.

    Only gathers tasks bound to the running loop — under pytest each test
    (and each TestClient portal) uses its own loop, so a straggler task from
    a previous test would otherwise raise "future belongs to a different
    loop". Foreign / done references are simply dropped.
    """
    loop = asyncio.get_event_loop()
    heartbeats = [task for task in _heartbeat_tasks if task.get_loop() is loop]
    for task in heartbeats:
        task.cancel()
    if heartbeats:
        await asyncio.gather(*heartbeats, return_exceptions=True)
    for task in list(_background_tasks):
        if task.get_loop() is not loop:
            _background_tasks.discard(task)
    while True:
        mine = [t for t in list(_background_tasks) if t.get_loop() is loop]
        if not mine:
            return
        await asyncio.gather(*mine, return_exceptions=True)


# Studio's own terminal state set (the on-wire ArtifactJob status is mapped
# from these — see ``_map_status``).
_TERMINAL_STATES = frozenset({"done", "failed", "cancelled"})

_STATUS_MAP = {
    "pending": "queued",
    "running": "running",
    "done": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}


@dataclass(frozen=True)
class ArtifactInfo:
    """Per-pipeline description of a job's output, produced by each service's
    ``artifact_info(record)`` helper.  Every terminal output must supply its
    actual primary bytes plus filename/MIME.  ``storage_ref`` is retained only
    to rebuild legacy Studio-local download links; CSP never trusts it.
    """

    artifact_type: str
    title: str | None = None
    storage_ref: str | None = None
    primary_bytes: bytes | None = None
    original_filename: str | None = None
    media_type: str | None = None
    result_metadata: dict[str, Any] = field(default_factory=dict)


class _JobUpdaterLike(Protocol):
    async def set(self, **kwargs: Any) -> None: ...


@dataclass
class JobReportContext:
    """The cross-cutting context threaded from an endpoint through a job.

    Built by the create endpoint (which alone knows the caller identity,
    bearer, and the additive task_id / source_snapshot_id / trace_id), then
    carried by the pipeline's updater. ``describe`` is the owning service's
    ``artifact_info`` function so the coordinator can stay generic.
    """

    artifact_type: str
    owner_user_id: int
    bearer: str
    collection_id: int | None = None
    requester: str | None = None
    task_id: str | None = None
    source_snapshot_id: str | None = None
    trace_id: str | None = None
    # Validated Pydantic request serialized without browser credentials.
    request_spec: dict[str, Any] = field(default_factory=dict)
    describe: Callable[[Any], ArtifactInfo | None] | None = None
    emitter: StudioTraceEmitter | None = None
    # Filled in once the artifact is registered on the control plane.
    classification_level: str | None = None
    artifact_id: int | None = None
    download_url: str | None = None
    # Internal guards (not part of the public surface).
    _terminal_reported: bool = False
    _last_step: str | None = None
    _create_report_task: asyncio.Task[None] | None = None
    _lease_token: str | None = None
    _attempt_count: int = 0
    _heartbeat_task: asyncio.Task[None] | None = None
    _durable_terminal: bool = False
    _synchronous_finalize: bool = False


def make_context(
    *,
    artifact_type: str,
    owner_user_id: int,
    requester: str | None,
    bearer: str,
    collection_id: int | None,
    describe: Callable[[Any], ArtifactInfo | None],
    task_id: str | None = None,
    source_snapshot_id: str | None = None,
    trace_id: str | None = None,
    request_spec: dict[str, Any] | None = None,
) -> JobReportContext:
    """Build a :class:`JobReportContext` for a create endpoint.

    Centralises the emitter wiring (endpoint = CSP base URL, producer =
    studio) so each of the five endpoints only imports this one factory.
    The emitter is inactive whenever ``trace_id`` is absent.
    """
    emitter = StudioTraceEmitter(
        trace_id=trace_id,
        endpoint=settings.CSP_BASE_URL,
        bearer=bearer,
        task_id=task_id,
    )
    return JobReportContext(
        artifact_type=artifact_type,
        owner_user_id=owner_user_id,
        bearer=bearer,
        collection_id=collection_id,
        requester=requester,
        task_id=task_id,
        source_snapshot_id=source_snapshot_id,
        trace_id=trace_id,
        request_spec=dict(request_spec or {}),
        describe=describe,
        emitter=emitter,
    )


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None:
        return None
    return str(value)


def _sha256(data: bytes | None) -> str | None:
    if not data:
        return None
    return hashlib.sha256(data).hexdigest()


def _map_status(state: str) -> str:
    return _STATUS_MAP.get(state, state)


def _project(
    record: Any,
    ctx: JobReportContext | None,
    *,
    base: PersistedJob | None = None,
) -> PersistedJob:
    """Build the durable ``PersistedJob`` projection from a live record."""
    status_view: dict[str, Any]
    try:
        status_view = record.to_status().model_dump(mode="json")
    except Exception:  # noqa: BLE001 — never let projection break a transition
        status_view = {}
    info: ArtifactInfo | None = None
    if ctx is not None and ctx.describe is not None:
        try:
            info = ctx.describe(record)
        except Exception:  # noqa: BLE001
            info = None
    artifact_type = (
        ctx.artifact_type if ctx is not None else (info.artifact_type if info else "unknown")
    )
    projected = PersistedJob(
        job_id=record.job_id,
        artifact_type=artifact_type,
        owner_user_id=record.user_id,
        state=record.state,
        status_view=status_view,
        collection_id=getattr(record, "collection_id", None),
        requester=(ctx.requester if ctx else None),
        task_id=(ctx.task_id if ctx else None),
        source_snapshot_id=(ctx.source_snapshot_id if ctx else None),
        trace_id=(ctx.trace_id if ctx else None),
        request_spec=(dict(ctx.request_spec) if ctx else {}),
        stage=getattr(record, "step", None),
        max_attempts=settings.STUDIO_JOB_MAX_ATTEMPTS,
        classification_level=(
            getattr(record, "classification_level", None)
            or (ctx.classification_level if ctx else None)
        ),
        artifact_id=(
            getattr(record, "artifact_id", None) or (ctx.artifact_id if ctx else None)
        ),
        storage_ref=(info.storage_ref if info else None),
        result_metadata=(dict(info.result_metadata) if info else {}),
        created_at=_iso(getattr(record, "created_at", None)),
        updated_at=_iso(getattr(record, "updated_at", None)),
    )
    if base is None:
        return projected
    # Queue ownership/counters are Redis authority and must survive every
    # status projection.  Only pipeline-visible fields are refreshed.
    return PersistedJob(
        **{
            **base.__dict__,
            "state": projected.state,
            "status_view": projected.status_view,
            "stage": projected.stage,
            "classification_level": projected.classification_level,
            "artifact_id": projected.artifact_id,
            "download_url": (ctx.download_url if ctx else base.download_url),
            "storage_ref": projected.storage_ref,
            "content_hash": projected.content_hash,
            "result_metadata": projected.result_metadata,
            "last_error": getattr(record, "error", None) or base.last_error,
            "updated_at": projected.updated_at,
        }
    )


async def _heartbeat_loop(job_id: str, ctx: JobReportContext) -> None:
    try:
        while ctx._lease_token and not ctx._durable_terminal:
            await asyncio.sleep(settings.STUDIO_JOB_HEARTBEAT_SECONDS)
            token = ctx._lease_token
            if not token or ctx._durable_terminal:
                return
            await get_job_store().heartbeat(
                job_id,
                token,
                lease_seconds=settings.STUDIO_JOB_LEASE_SECONDS,
            )
            await job_reporting.report_job_lease(
                job_id=job_id,
                attempt=ctx._attempt_count,
                lease_token=token,
            )
    except asyncio.CancelledError:
        raise
    except LeaseLost:
        logger.error("Studio job %s lost its durable queue lease", job_id)
    except Exception:
        logger.exception("Studio job %s heartbeat failed", job_id)


def _start_heartbeat(job_id: str, ctx: JobReportContext) -> None:
    task = asyncio.create_task(
        _heartbeat_loop(job_id, ctx),
        name=f"studio-job-heartbeat-{job_id}",
    )
    ctx._heartbeat_task = task
    _heartbeat_tasks.add(task)
    task.add_done_callback(_heartbeat_tasks.discard)


async def _stop_heartbeat(ctx: JobReportContext) -> None:
    task, ctx._heartbeat_task = ctx._heartbeat_task, None
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _reporting_on(ctx: JobReportContext | None) -> bool:
    return ctx is not None and settings.STUDIO_ARTIFACT_REPORTING


async def on_create(record: Any, ctx: JobReportContext | None) -> None:
    """Persist the initial record + open the trace root + POST artifact-job.

    The store persist is awaited inline (fast, and the durability guarantee).
    The CSP POST is detached so a slow/unreachable CSP never delays the 202.
    """
    if ctx is None:
        # Direct unit-level pipeline invocation is not an HTTP acceptance
        # path.  Every production create endpoint supplies a context.
        return
    if is_formal_profile():
        try:
            collection_id = int(ctx.collection_id or 0)
            task_id = int(ctx.task_id or 0)
            snapshot_id = int(ctx.source_snapshot_id or 0)
        except (TypeError, ValueError) as exc:
            raise DurableBindingError("正式 artifact 必須綁正整數 collection/task/snapshot") from exc
        if min(collection_id, task_id, snapshot_id) < 1 or not (ctx.trace_id or "").strip():
            raise DurableBindingError(
                "正式 artifact 必須綁正整數 collection/task/snapshot 與 Full Trace"
            )
        if not ctx.request_spec:
            raise DurableBindingError("正式 artifact 缺少可重播 request_spec")
    if settings.STUDIO_DURABLE_SUPERVISOR:
        await get_job_store().enqueue(_project(record, ctx))
    if not _reporting_on(ctx):
        return
    assert ctx is not None
    if ctx.emitter is not None and ctx.emitter.active:
        ctx.emitter.start_root(
            f"studio.{ctx.artifact_type}",
            attributes={"job_id": record.job_id, "collection_id": ctx.collection_id},
        )
    if settings.STUDIO_DURABLE_SUPERVISOR:
        # The supervisor idempotently creates/reads back the CSP ArtifactJob
        # after claiming the durable envelope. No request closure survives.
        return
    ctx._create_report_task = _spawn(
        job_reporting.report_job_created(
            bearer=ctx.bearer,
            job_id=record.job_id,
            artifact_type=ctx.artifact_type,
            status=_map_status(record.state),
            requester_user_id=ctx.owner_user_id,
            employee_id=ctx.requester,
            task_id=ctx.task_id,
            source_snapshot_id=ctx.source_snapshot_id,
            trace_id=ctx.trace_id,
        )
    )


async def on_transition(
    record: Any, ctx: JobReportContext | None, updater: _JobUpdaterLike | None
) -> None:
    """Persist the record on every transition; on terminal, report to CSP.

    Only the store persist runs inline (fast). The terminal CSP calls +
    span flush run detached (``_spawn``) so their retries never keep the
    pipeline task busy after the job is done — a cancellation during that
    window would otherwise flip a completed job to "cancelled".
    """
    if ctx is None:
        return
    store = get_job_store()
    if settings.STUDIO_DURABLE_SUPERVISOR and not ctx._durable_terminal:
        current: PersistedJob | None
        if record.state == "running" and ctx._lease_token is None:
            current = await store.claim_job(
                record.job_id,
                worker_id=_WORKER_ID,
                lease_seconds=settings.STUDIO_JOB_LEASE_SECONDS,
            )
            if current is None or current.lease_token is None:
                raise LeaseLost(record.job_id)
            ctx._lease_token = current.lease_token
            _start_heartbeat(record.job_id, ctx)
        else:
            current = await store.get(record.job_id)
        if current is None or current.lease_token != ctx._lease_token:
            raise LeaseLost(record.job_id)

        projected = _project(record, ctx, base=current)
        if record.state == "done":
            # Generated bytes are not a durable success. Keep the envelope
            # running until CSP returns artifact_id + download_url.
            pending_view = dict(projected.status_view)
            pending_view["state"] = "running"
            projected = PersistedJob(
                **{
                    **projected.__dict__,
                    "state": "running",
                    "stage": "registering_artifact",
                    "status_view": pending_view,
                }
            )
            await store.update_claimed(projected, ctx._lease_token)
        elif record.state in {"failed", "cancelled"}:
            pending_view = dict(projected.status_view)
            pending_view["state"] = "running"
            projected = PersistedJob(
                **{
                    **projected.__dict__,
                    "state": "running",
                    "stage": "terminal_report_pending",
                    "status_view": pending_view,
                    "checkpoint": {
                        **projected.checkpoint,
                        "terminal_status": record.state,
                        "local_terminal_state": record.state,
                    },
                }
            )
            await store.update_claimed(projected, ctx._lease_token)
        else:
            await store.update_claimed(projected, ctx._lease_token)
    if not _reporting_on(ctx):
        return
    assert ctx is not None

    step = getattr(record, "step", None)
    if (
        ctx.emitter is not None
        and ctx.emitter.active
        and step
        and step != ctx._last_step
    ):
        ctx._last_step = step
        ctx.emitter.stage(step)

    state = record.state
    if state not in _TERMINAL_STATES or ctx._terminal_reported:
        return
    ctx._terminal_reported = True
    if ctx._synchronous_finalize:
        await _finalize(record, ctx, updater)
    else:
        _spawn(_finalize(record, ctx, updater))


async def _finalize(
    record: Any, ctx: JobReportContext, updater: _JobUpdaterLike | None
) -> None:
    """Register artifact and terminal state; fail local completion closed."""
    state = record.state
    artifact_id: int | None = None
    classification_level: str | None = None

    # The CSP job row is a prerequisite for artifact registration. A very
    # fast pipeline may reach terminal while the detached create POST is still
    # in flight, so establish an explicit per-job happens-before edge here.
    create_task = ctx._create_report_task
    if create_task is not None:
        try:
            await asyncio.shield(create_task)
        except asyncio.CancelledError:
            # Cancellation of this finalize task must not cancel the shared
            # create POST. A directly-cancelled create task is fail-closed.
            if not create_task.cancelled():
                raise
            message = "產物控制面 job 建立失敗:CancelledError"
            job_reporting.mark_reporting_failure(message)
            if updater is not None:
                await updater.set(state="failed", error=message)
            return
        except Exception as exc:  # noqa: BLE001 - prerequisite is fail-closed
            message = f"產物控制面 job 建立失敗:{type(exc).__name__}"
            job_reporting.mark_reporting_failure(message)
            if updater is not None:
                await updater.set(state="failed", error=message)
            return
        finally:
            ctx._create_report_task = None

    if state == "done" and ctx.describe is not None:
        info: ArtifactInfo | None = None
        try:
            info = ctx.describe(record)
        except Exception:  # noqa: BLE001
            info = None
        try:
            if (
                info is None
                or not info.primary_bytes
                or not info.original_filename
                or not info.media_type
            ):
                raise job_reporting.ArtifactReportingError(
                    "completed Studio job 缺少 authoritative bytes/filename/MIME"
                )
            content_hash = _sha256(info.primary_bytes)
            if content_hash is None:
                raise job_reporting.ArtifactReportingError("authoritative bytes 不可為空")
            result = await job_reporting.register_artifact(
                bearer=ctx.bearer,
                job_id=record.job_id,
                task_id=ctx.task_id,
                source_snapshot_id=ctx.source_snapshot_id,
                artifact_type=ctx.artifact_type,
                title=info.title,
                content=info.primary_bytes,
                original_filename=info.original_filename,
                media_type=info.media_type,
                content_hash=content_hash,
                classification_level=None,
                metadata=info.result_metadata,
                attempt=ctx._attempt_count,
                lease_token=ctx._lease_token,
            )
            artifact_id = result.get("artifact_id")
            download_url = result.get("download_url")
            if not isinstance(artifact_id, int) or not isinstance(download_url, str) or not download_url:
                raise job_reporting.ArtifactReportingError(
                    "CSP 回應缺少 artifact_id/download_url"
                )
            classification_level = result.get("classification_level")
            ctx.artifact_id = artifact_id
            ctx.download_url = download_url
            ctx.classification_level = classification_level
        except job_reporting.ArtifactReportingError as exc:
            message = f"產物控制面註冊失敗:{exc}"
            if updater is not None:
                await updater.set(state="failed", error=message)
            try:
                await job_reporting.report_job_patched(
                    bearer=ctx.bearer,
                    job_id=record.job_id,
                    status="failed",
                    error={
                        "code": "artifact_control_plane_error",
                        "message": message,
                    },
                )
            except job_reporting.ArtifactReportingError:
                job_reporting.mark_reporting_failure(message)
                raise
            if settings.STUDIO_DURABLE_SUPERVISOR:
                token = ctx._lease_token
                current = await get_job_store().get(record.job_id)
                if token is None or current is None:
                    raise LeaseLost(record.job_id)
                failed_view = dict(current.status_view)
                failed_view.update({"state": "failed", "error": message})
                await _stop_heartbeat(ctx)
                await get_job_store().complete(
                    PersistedJob(
                        **{
                            **current.__dict__,
                            "state": "failed",
                            "stage": "failed",
                            "status_view": failed_view,
                            "checkpoint": {},
                            "last_error": message,
                        }
                    ),
                    token,
                )
                ctx._durable_terminal = True
                ctx._lease_token = None
            job_reporting.mark_reporting_failure(message)
            return

    await job_reporting.report_job_patched(
        bearer=ctx.bearer,
        job_id=record.job_id,
        status=_map_status(state),
        error=getattr(record, "error", None),
        artifact_id=artifact_id,
    )

    if state in _TERMINAL_STATES and settings.STUDIO_DURABLE_SUPERVISOR:
        token = ctx._lease_token
        current = await get_job_store().get(record.job_id)
        if token is None or current is None:
            raise LeaseLost(record.job_id)
        durable = _project(record, ctx, base=current)
        durable = PersistedJob(
            **{
                **durable.__dict__,
                "state": state,
                "artifact_id": artifact_id,
                "download_url": ctx.download_url,
                "checkpoint": {},
            }
        )
        await _stop_heartbeat(ctx)
        await get_job_store().complete(durable, token)
        ctx._durable_terminal = True
        ctx._lease_token = None

    if ctx.emitter is not None and ctx.emitter.active:
        ctx.emitter.finish(status="ok" if state == "done" else "error")
        await ctx.emitter.flush()

    # Write the registered artifact metadata back onto the live record so a
    # cache-hit status also carries it (and re-persists the enriched view via
    # the re-entrant on_transition — guarded by _terminal_reported).
    if updater is not None and (artifact_id or classification_level):
        await updater.set(
            artifact_id=artifact_id, classification_level=classification_level
        )


async def read_status(job_id: str, user_id: int) -> dict[str, Any] | None:
    """Read-through for a status poll after the in-memory record is gone.

    Returns the persisted ``status_view`` (the verbatim ``to_status()``
    JSON) when a durable record exists for ``job_id`` owned by ``user_id``;
    ``None`` otherwise (missing / cross-user → the endpoint 404s).
    """
    try:
        persisted = await get_job_store().get(job_id)
    except JobStoreUnavailable:
        if is_formal_profile():
            raise
        return None
    if persisted is None or persisted.owner_user_id != user_id:
        return None
    return persisted.status_view
