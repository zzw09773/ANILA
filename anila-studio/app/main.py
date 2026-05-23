"""anila-studio FastAPI app entrypoint.

Wave-1 subagents populate:
- app.services.jwks_client    — fetch + cache csp JWKS
- app.services.revocation_cache — Redis subscriber for cross-service token revoke
- app.clients.csp_client       — thin HTTP wrapper around csp

This file holds the FastAPI() instance + /health endpoint as a smoke target;
Wave-2 wiring will:
- attach lifespan handlers to start the revocation subscriber
- mount /api/studio router (once Phase 3 ports it over)
- add readiness gate to /health (JWKS fetched + revocation cache cold-started)
"""

from __future__ import annotations

from fastapi import FastAPI

from app.config import settings


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
)


@app.get("/health")
async def health() -> dict[str, object]:
    """Liveness check. Wave-2 will add `ready` flag once lifespan deps wire up."""
    return {
        "status": "ok",
        "service": settings.APP_NAME,
        "version": settings.APP_VERSION,
    }
