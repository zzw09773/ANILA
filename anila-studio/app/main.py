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
from app.config import settings
from app.services import jwks_client, revocation_cache as revocation_cache_mod


logger = logging.getLogger(__name__)


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
    logger.info("[%s] startup complete; serving traffic", settings.APP_NAME)
    try:
        yield
    finally:
        logger.info("[%s] shutdown: stopping background tasks", settings.APP_NAME)
        await revocation_cache.stop(app)
        await jwks_client.stop(app)
        logger.info("[%s] shutdown complete", settings.APP_NAME)


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
)
app.include_router(studio_router)


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
