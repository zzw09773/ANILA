"""anila-studio FastAPI app entrypoint.

Lifecycle:
- ``startup``  — fetch csp JWKS, cold-start revocation cache from
                 csp /api/auth/revocations, subscribe to Redis pub/sub
                 events. Either failure → app refuses to be ready.
- ``shutdown`` — gracefully cancel background tasks.

``/health`` returns:
- ``200 {"status":"ok","ready":true,...}`` once both dependencies are ready
- ``503 {"status":"degraded","ready":false,...}`` if either is not

Phase 3 wires the studio router (``/api/studio/...``); the lifecycle +
health surface itself remains unchanged.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.api.studio import router as studio_router
from app.api.reports import router as reports_router
from app.api.mindmaps import router as mindmaps_router
from app.api.infographics import router as infographics_router
from app.api.datatables import router as datatables_router
from app.config import settings
from app.services import jwks_client, revocation_cache as revocation_cache_mod
from app.services.job_store import get_job_store


logger = logging.getLogger(__name__)


def configure_logging(level_name: str | None = None) -> int:
    """Apply ``settings.LOG_LEVEL`` to the root logger once.

    Until 2026-09-02 nothing ever called ``basicConfig``, so every
    ``logger.info`` in the pipeline (layout audit, rebalance, s2tw diffs,
    ``[H-DIAG]``) was silently dropped and ``LOG_LEVEL`` did nothing.
    uvicorn installs its own handlers for its own loggers; we only touch the
    root so those are not doubled.
    """
    name = (level_name or settings.LOG_LEVEL or "INFO").upper()
    level = getattr(logging, name, logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
    root.setLevel(level)
    return level


configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start + stop the cross-service auth deps.

    A failure during startup propagates and the FastAPI process exits — that
    is the correct behaviour for a service that cannot verify identity. k8s
    / docker will restart and try again.
    """
    logger.info("[%s] startup: bringing up JWKS + revocation cache", settings.APP_NAME)
    await jwks_client.start(app)
    revocation_cache = revocation_cache_mod.get_revocation_cache()
    await revocation_cache.start(app)
    # Durable job store (Slice 8b) — best-effort: a Redis outage degrades
    # jobs to in-memory only (restart-survival lost) but does NOT block
    # startup, unlike the revocation cache which is fail-closed.
    job_store = get_job_store()
    await job_store.start(app)
    logger.info("[%s] startup complete; serving traffic", settings.APP_NAME)
    try:
        yield
    finally:
        logger.info("[%s] shutdown: stopping background tasks", settings.APP_NAME)
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

    ``revocation_cache.ready`` flips to False if Redis disconnects; the
    /health endpoint reports 503 in that window so k8s readiness probes
    pull the pod out of the load balancer until it recovers.

    jwks_client does not expose ``ready`` directly (it has a fallback
    to lazy-fetch on first verify) but its cold-start happens in lifespan
    so by the time we serve traffic it has at least one key cached.
    """
    revocation_cache = revocation_cache_mod.get_revocation_cache()
    ready = revocation_cache.ready
    body = {
        "status": "ok" if ready else "degraded",
        "service": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "ready": ready,
        "deps": {
            "revocation_cache": ready,
        },
    }
    return JSONResponse(
        status_code=200 if ready else 503,
        content=body,
    )
