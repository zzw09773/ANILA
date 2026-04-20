"""Rate-limit middleware for data-plane (API-key authenticated) proxy requests.

Applies only to paths under /v1/ (the data plane). Control-plane /api/ routes
use JWT auth and are intentionally excluded from key-based rate limiting.
"""

from __future__ import annotations

import logging

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.database import SessionLocal
from app.models.api_key import ApiKey
from app.services.api_key_service import validate_api_key
from app.services.rate_limit_service import check_and_record

logger = logging.getLogger(__name__)

_DATA_PLANE_PREFIX = "/v1/"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Enforce quota policies on /v1/* endpoints."""

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        if not path.startswith(_DATA_PLANE_PREFIX):
            return await call_next(request)

        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return await call_next(request)

        raw_key = auth[7:]

        db = SessionLocal()
        try:
            api_key: ApiKey | None = validate_api_key(db, raw_key)
            if api_key is None:
                return await call_next(request)

            result = check_and_record(db, api_key)
        except Exception as exc:
            logger.warning("Rate-limit check error (key=%s…): %s", raw_key[:8], exc)
            return await call_next(request)
        finally:
            db.close()

        if not result.allowed:
            logger.info(
                "Rate-limit blocked api_key=%d path=%s reason=%s",
                api_key.id,
                path,
                result.reason,
            )
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "type": "rate_limit_exceeded",
                        "message": result.reason,
                        "code": "rate_limit_exceeded",
                    }
                },
                headers={"Retry-After": "60"},
            )

        return await call_next(request)
