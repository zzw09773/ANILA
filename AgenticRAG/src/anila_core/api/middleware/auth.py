"""Service-to-service authentication middleware for ANILA Core API.

When deployed behind myCSPPlatform, requests must carry:
    X-CSP-Service-Token: <csp_service_token>

The real user identity arrives via trusted forwarded headers injected by CSP:
    X-ANILA-User-Id, X-ANILA-User-Email, X-ANILA-User-Groups

Auth is skipped when:
  - dev_mode is True
  - service_token is None/empty (local dev without CSP)
  - The request path is /health or /docs or /openapi.json
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

_PUBLIC_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}


class CspServiceTokenMiddleware(BaseHTTPMiddleware):
    """Validate that the request originates from myCSPPlatform."""

    def __init__(self, app, service_token: str | None, dev_mode: bool = False) -> None:
        super().__init__(app)
        self._service_token = service_token
        self._dev_mode = dev_mode

    async def dispatch(self, request: Request, call_next):
        if self._dev_mode or not self._service_token:
            return await call_next(request)

        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        token = request.headers.get("X-CSP-Service-Token", "")
        if not token:
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing X-CSP-Service-Token header"},
            )

        if token != self._service_token:
            return JSONResponse(
                status_code=403,
                content={"detail": "Invalid service token"},
            )

        return await call_next(request)


# Keep the old name as an alias so existing code that imports ApiKeyMiddleware
# doesn't break immediately — will be removed once all callers migrate.
ApiKeyMiddleware = CspServiceTokenMiddleware
