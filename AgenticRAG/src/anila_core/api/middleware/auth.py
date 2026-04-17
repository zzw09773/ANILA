"""Bearer token authentication middleware for ANILA Core API.

When api_key is set in config, every request must include:
    Authorization: Bearer <api_key>

Auth is skipped when:
  - config.api_dev_mode is True
  - config.api_key is None or empty
  - The request path is /health or /docs or /openapi.json
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

_PUBLIC_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Validate Bearer token on all non-public endpoints."""

    def __init__(self, app, api_key: str | None, dev_mode: bool = False) -> None:
        super().__init__(app)
        self._api_key = api_key
        self._dev_mode = dev_mode

    async def dispatch(self, request: Request, call_next):
        if self._dev_mode or not self._api_key:
            return await call_next(request)

        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid Authorization header"},
            )

        token = auth_header[7:]  # strip "Bearer "
        if token != self._api_key:
            return JSONResponse(
                status_code=403,
                content={"detail": "Invalid API key"},
            )

        return await call_next(request)
