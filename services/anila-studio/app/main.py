"""anila-studio FastAPI app entrypoint.

Lifecycle:
- ``startup``  — fetch csp JWKS, cold-start revocation cache from
                 csp /api/auth/revocations, subscribe to Redis pub/sub
                 events, and probe the named artifact writer capability.
                 Any failure → app refuses to be ready.
- ``shutdown`` — gracefully cancel background tasks.

``/health`` returns:
- ``200 {"status":"ok","ready":true,...}`` once both dependencies are ready
- ``503 {"status":"degraded","ready":false,...}`` if either is not

Phase 3 wires the studio router (``/api/studio/...``); the lifecycle +
health surface itself remains unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.api.studio import router as studio_router
from app.api.reports import router as reports_router
from app.api.mindmaps import router as mindmaps_router
from app.api.infographics import router as infographics_router
from app.api.datatables import router as datatables_router
from app.config import assert_durable_startup_posture, is_formal_profile, settings
from app.services import (
    job_lifecycle,
    job_reporting,
    jwks_client,
    revocation_cache as revocation_cache_mod,
)
from app.services.job_store import JobStoreUnavailable, get_job_store
from app.services.job_supervisor import get_job_supervisor


logger = logging.getLogger(__name__)


async def _job_store_readiness_loop() -> None:
    """Reconnect/reconcile the durable queue after a transient outage."""
    while True:
        try:
            await asyncio.sleep(settings.STUDIO_JOB_READINESS_PROBE_SECONDS)
            await get_job_store().probe()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - readiness stays false and retries
            logger.exception("durable Studio queue readiness probe failed")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start + stop the cross-service auth deps.

    A failure during startup propagates and the FastAPI process exits — that
    is the correct behaviour for a service that cannot verify identity. k8s
    / docker will restart and try again.
    """
    logger.info("[%s] startup: bringing up JWKS + revocation cache", settings.APP_NAME)
    assert_durable_startup_posture()
    await jwks_client.start(app)
    revocation_cache = revocation_cache_mod.get_revocation_cache()
    await revocation_cache.start(app)
    # Durable queue is fail-closed for job acceptance. Startup stays alive for
    # diagnostics; readiness remains false until the reconnect loop succeeds.
    job_store = get_job_store()
    await job_store.start(app)
    job_store_readiness_task = asyncio.create_task(
        _job_store_readiness_loop(),
        name="studio-job-store-readiness",
    )
    # A failed probe keeps the process alive for diagnosis/recovery but marks
    # readiness degraded. No artifact request is accepted as healthy while the
    # named writer credential/capability is rejected or unreachable.
    await job_reporting.probe_readiness()
    artifact_readiness_task = job_reporting.start_readiness_monitor()
    supervisor = get_job_supervisor()
    await supervisor.start()
    logger.info("[%s] startup complete; serving traffic", settings.APP_NAME)
    try:
        yield
    finally:
        logger.info("[%s] shutdown: stopping background tasks", settings.APP_NAME)
        job_store_readiness_task.cancel()
        await asyncio.gather(job_store_readiness_task, return_exceptions=True)
        await supervisor.stop()
        await job_reporting.stop_readiness_monitor(artifact_readiness_task)
        await job_lifecycle.drain()
        await job_store.stop(app)
        await revocation_cache.stop(app)
        await jwks_client.stop(app)
        logger.info("[%s] shutdown complete", settings.APP_NAME)


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
)
app.include_router(studio_router)
# 4 種新 artifact kind(對應前端製作台 menu 內已 implemented + 3 個 comingSoon 轉正)
app.include_router(reports_router)
app.include_router(mindmaps_router)
app.include_router(infographics_router)
app.include_router(datatables_router)


@app.get("/health")
async def health() -> JSONResponse:
    """Liveness + readiness gate.

    ``revocation_cache.ready`` or artifact reporting readiness flips False;
    /health endpoint reports 503 in that window so k8s readiness probes
    pull the pod out of the load balancer until it recovers.

    jwks_client does not expose ``ready`` directly (it has a fallback
    to lazy-fetch on first verify) but its cold-start happens in lifespan
    so by the time we serve traffic it has at least one key cached.
    """
    revocation_cache = revocation_cache_mod.get_revocation_cache()
    revocation_ready = revocation_cache.ready
    artifact_reporting = job_reporting.reporting_status()
    artifact_ready = bool(artifact_reporting["ready"])
    durable_jobs = get_job_store()
    durable_jobs_ready = durable_jobs.ready
    supervisor_ready = (
        get_job_supervisor().running
        if is_formal_profile()
        else (
            not settings.STUDIO_DURABLE_SUPERVISOR
            or get_job_supervisor().running
        )
    )
    ready = (
        revocation_ready
        and artifact_ready
        and durable_jobs_ready
        and supervisor_ready
    )
    body = {
        "status": "ok" if ready else "degraded",
        "service": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "ready": ready,
        "deps": {
            "revocation_cache": revocation_ready,
            "artifact_reporting": artifact_reporting,
            "durable_jobs": {
                "ready": durable_jobs_ready,
                "last_error": durable_jobs.last_error,
            },
            "job_supervisor": supervisor_ready,
        },
    }
    return JSONResponse(
        status_code=200 if ready else 503,
        content=body,
    )


@app.exception_handler(JobStoreUnavailable)
async def durable_job_store_unavailable(
    _request: Request,
    _exc: JobStoreUnavailable,
) -> JSONResponse:
    """Accepted work must never fall back to process-local state."""
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "detail": "Studio durable job queue unavailable",
            "retryable": True,
        },
        headers={"Retry-After": "5"},
    )


@app.exception_handler(job_lifecycle.DurableBindingError)
async def durable_binding_invalid(
    _request: Request,
    exc: job_lifecycle.DurableBindingError,
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": str(exc)},
    )
