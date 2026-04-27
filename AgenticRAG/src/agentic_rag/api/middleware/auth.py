"""Bearer token authentication middleware for AgenticRAG API.

Sprint 5 X security review (H3): the previous implementation passed every
request through whenever ``api_key`` was unset / empty — which combined
with the default ``API_KEY=`` in ``.env.example`` left the entire API
unauthenticated by accident. We now fail-closed unless the operator
explicitly opts into ``api_dev_mode``, and use a constant-time comparison
for the bearer token.

Auth is skipped only when:
  - config.api_dev_mode is True (explicit local-dev opt-in), or
  - the request path is one of ``_PUBLIC_PATHS``.

When ``api_key`` is not configured and dev mode is off, every non-public
request returns 503 Service Unavailable rather than silently letting the
caller through.
"""

from __future__ import annotations

import hmac

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

_PUBLIC_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Validate Bearer token on all non-public endpoints (fail-closed)."""

    def __init__(self, app, api_key: str | None, dev_mode: bool = False) -> None:
        super().__init__(app)
        self._api_key = (api_key or "").strip()
        self._dev_mode = bool(dev_mode)

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        if self._dev_mode:
            return await call_next(request)

        if not self._api_key:
            # Fail-closed: empty / missing config → refuse instead of pass-through.
            return JSONResponse(
                status_code=503,
                content={
                    "detail": (
                        "AgenticRAG API_KEY 尚未設定。請於 .env 設定 API_KEY，"
                        "或啟動時設 API_DEV_MODE=true 以僅供本機開發使用。"
                    )
                },
            )

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid Authorization header"},
            )

        token = auth_header[7:]  # strip "Bearer "
        # Constant-time compare to avoid timing-side-channel hints about the
        # configured API key prefix.
        if not hmac.compare_digest(token.encode("utf-8"), self._api_key.encode("utf-8")):
            return JSONResponse(
                status_code=403,
                content={"detail": "Invalid API key"},
            )

        return await call_next(request)
