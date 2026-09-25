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
``trace_id`` rides on the artifact-job body as a correlation id.
It is not posted to ``/v1/traces``.

CSP reporting is fire-and-forget: CSP being down must never break
generation. Persistence (1) is best-effort too (``put_quietly``). The
pipeline itself is unchanged — the endpoints build a
:class:`JobReportContext`, hand it to ``create_job``, and the updater
calls into here.

Does NOT import ``anila_core``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol

from app.config import settings
from app.services import job_reporting
from app.services.job_store import PersistedJob, get_job_store



logger = logging.getLogger(__name__)


# Detached background tasks for the CSP reporting + span flush. These must
# NOT run inside the pipeline's asyncio task: their retry sleeps would keep
# the task alive after the job is logically done, and a cancellation during
# that window would flip the job to "cancelled". Running them detached keeps
# reporting fully decoupled from generation. We hold strong references so
# the tasks aren't GC'd mid-flight; ``drain()`` lets tests flush them.
_background_tasks: set[asyncio.Task[Any]] = set()


def _spawn(coro: Awaitable[None]) -> None:
    task = asyncio.ensure_future(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def drain() -> None:
    """Await all in-flight reporting tasks on the current loop. Test helper.

    Only gathers tasks bound to the running loop — under pytest each test
    (and each TestClient portal) uses its own loop, so a straggler task from
    a previous test would otherwise raise "future belongs to a different
    loop". Foreign / done references are simply dropped.
    """
    loop = asyncio.get_event_loop()
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
    ``artifact_info(record)`` helper. Only ``storage_ref`` being set (at
    terminal ``done``) triggers the POST /v1/artifacts register.

    ``primary_bytes`` is supplied only when the primary output is already in
    memory (slides pptx, mindmap svg) so hashing is cheap; disk-only outputs
    leave it ``None`` and skip the content hash.
    """

    artifact_type: str
    title: str | None = None
    storage_ref: str | None = None
    primary_bytes: bytes | None = None
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
    describe: Callable[[Any], ArtifactInfo | None] | None = None
    # Filled in once the artifact is registered on the control plane.
    classification_level: str | None = None
    artifact_id: str | None = None
    # Internal guards (not part of the public surface).
    _terminal_reported: bool = False
    _last_step: str | None = None


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
) -> JobReportContext:
    """Build a :class:`JobReportContext` for a create endpoint.

    ``trace_id`` is a correlation id on the artifact job, not a span export.
    """
    return JobReportContext(
        artifact_type=artifact_type,
        owner_user_id=owner_user_id,
        bearer=bearer,
        collection_id=collection_id,
        requester=requester,
        task_id=task_id,
        source_snapshot_id=source_snapshot_id,
        trace_id=trace_id,
        describe=describe,
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


def _project(record: Any, ctx: JobReportContext | None) -> PersistedJob:
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
    return PersistedJob(
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


def _reporting_on(ctx: JobReportContext | None) -> bool:
    return ctx is not None and settings.STUDIO_ARTIFACT_REPORTING


async def on_create(record: Any, ctx: JobReportContext | None) -> None:
    """Persist the initial record + open the trace root + POST artifact-job.

    The store persist is awaited inline (fast, and the durability guarantee).
    The CSP POST is detached so a slow/unreachable CSP never delays the 202.
    """
    await get_job_store().put_quietly(_project(record, ctx))
    if not _reporting_on(ctx):
        return
    assert ctx is not None
    _spawn(
        job_reporting.report_job_created(
            bearer=ctx.bearer,
            job_id=record.job_id,
            artifact_type=ctx.artifact_type,
            status=_map_status(record.state),
            requester_user_id=ctx.owner_user_id,
            employee_id=(ctx.requester if ctx.requester and not str(ctx.requester).isdigit() else None),
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
    await get_job_store().put_quietly(_project(record, ctx))
    if not _reporting_on(ctx):
        return
    assert ctx is not None

    state = record.state
    if state not in _TERMINAL_STATES or ctx._terminal_reported:
        return
    ctx._terminal_reported = True
    _spawn(_finalize(record, ctx, updater))


async def _finalize(
    record: Any, ctx: JobReportContext, updater: _JobUpdaterLike | None
) -> None:
    """Detached terminal reporting: register artifact → PATCH job → ship spans.

    Runs OUTSIDE the pipeline task (see ``on_transition``). All network here
    is fire-and-forget; failures are swallowed inside the reporter.
    """
    state = record.state
    artifact_id: str | None = None
    classification_level: str | None = None

    if state == "done" and ctx.describe is not None:
        info: ArtifactInfo | None = None
        try:
            info = ctx.describe(record)
        except Exception:  # noqa: BLE001
            info = None
        if info is not None and info.storage_ref:
            result = await job_reporting.register_artifact(
                bearer=ctx.bearer,
                job_id=record.job_id,
                task_id=ctx.task_id,
                source_snapshot_id=ctx.source_snapshot_id,
                artifact_type=ctx.artifact_type,
                title=info.title,
                storage_ref=info.storage_ref,
                content_hash=_sha256(info.primary_bytes),
                classification_level=None,
                metadata=info.result_metadata,
            )
            if result:
                artifact_id = result.get("artifact_id")
                classification_level = result.get("classification_level")
                ctx.artifact_id = artifact_id
                ctx.classification_level = classification_level

    await job_reporting.report_job_patched(
        bearer=ctx.bearer,
        job_id=record.job_id,
        status=_map_status(state),
        error=getattr(record, "error", None),
        artifact_id=artifact_id,
    )

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
    persisted = await get_job_store().get(job_id)
    if persisted is None or persisted.owner_user_id != user_id:
        return None
    return persisted.status_view
