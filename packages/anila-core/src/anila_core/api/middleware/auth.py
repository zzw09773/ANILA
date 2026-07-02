"""Service-to-service authentication middleware for ANILA Core API.

When deployed behind myCSPPlatform, requests must carry:
    X-CSP-Service-Token: <csp_service_token>

The real user identity arrives via trusted forwarded headers injected by CSP:
    X-ANILA-User-Id, X-ANILA-User-Email, X-ANILA-User-Groups

Two middlewares live in this module:

``CspServiceTokenMiddleware``
    Original v1 form — verifies the incoming header against a fixed
    ``service_token`` passed at construction time. Sprint 8 X / Phase B
    upgrades the comparison to ``hmac.compare_digest`` (constant-time)
    and keeps the rest of the surface unchanged so old call sites
    don't break.

``RotatingServiceTokenMiddleware``
    Sprint 8 X / Phase B — reads the agent's current token from a
    state file (``{state_dir}/service_token.json``, mode 0600), falls
    back to the legacy ``CSP_SERVICE_TOKEN`` env var when the file is
    missing, and reloads the state file on a single rejection so
    admin-side rotation propagates without a process restart. This is
    what AgenticRAG-template-based agents use after running
    ``anila-core agent bootstrap``.

Auth is skipped when:
  - ``dev_mode`` is True
  - For ``CspServiceTokenMiddleware``: ``service_token`` is empty.
  - For ``RotatingServiceTokenMiddleware``: NO token source is
    available (state file missing AND env var empty) — middleware
    logs a warning and lets all requests through. This matches the
    "local dev without CSP" deployment story.
  - The request path is in ``_PUBLIC_PATHS`` (always passes through).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from pathlib import Path
from threading import Lock
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


logger = logging.getLogger(__name__)

_PUBLIC_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}

# Default location for the agent's per-agent service token state.
# Containerised deployments mount a named volume here (see
# AgenticRAG/Dockerfile). Override by setting ``ANILA_AGENT_STATE_DIR``
# at process startup.
DEFAULT_STATE_DIR = "/var/lib/anila-agent"
STATE_FILE_NAME = "service_token.json"


# ---------------------------------------------------------------------------
# CspServiceTokenMiddleware (legacy / static)
# ---------------------------------------------------------------------------


class CspServiceTokenMiddleware(BaseHTTPMiddleware):
    """Validate that the request originates from myCSPPlatform.

    Sprint 8 X / Phase B — token comparison upgraded to
    ``hmac.compare_digest`` so a token that differs in only the first
    bytes can't be inferred from response timing. Behaviour is
    otherwise unchanged.
    """

    def __init__(self, app, service_token: str | None, dev_mode: bool = False) -> None:
        super().__init__(app)
        self._service_token = service_token or ""
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

        if not hmac.compare_digest(token, self._service_token):
            return JSONResponse(
                status_code=403,
                content={"detail": "Invalid service token"},
            )

        return await call_next(request)


# ---------------------------------------------------------------------------
# RotatingServiceTokenMiddleware (Sprint 8 X / Phase B)
# ---------------------------------------------------------------------------


class RotatingServiceTokenMiddleware(BaseHTTPMiddleware):
    """State-file-backed service token middleware with hot reload.

    Lifecycle
    ---------
    1. ``__init__`` resolves the token source in this priority order:
         a. ``state_dir/service_token.json`` (written by
            ``anila-core agent bootstrap`` CLI).
         b. ``env_token`` arg or ``CSP_SERVICE_TOKEN`` env var.
         c. Nothing — middleware enters "local dev" mode (passes all
            requests through with a warning at startup).

    2. On each request the middleware compares the incoming header
       to the in-memory token using ``hmac.compare_digest``.

    3. On a single mismatch, the middleware **reloads the state file
       once** (covers the case "admin rotated the token; CLI rewrote
       the state file; agent process is still holding the old
       in-memory copy"). If the reloaded token also doesn't match,
       the request is rejected with 403.

    State file format
    -----------------
    ::

        {
          "token": "csk-...",
          "previous_token": null,            # optional, for explicit grace
          "previous_expires_at": null,
          "agent_id": 2,
          "agent_name": "agentic-rag",
          "csp_url": "http://csp:8000",
          "label": "pod-1",
          "issued_at": "2026-04-30T12:34:56Z"
        }

    Concurrency
    -----------
    A ``threading.Lock`` guards reloads — under heavy traffic with a
    rotation event, every worker that sees the rejection may try to
    reload at once. Lock contention is microsecond-level; the lock
    just prevents duplicate disk reads. We deliberately do NOT use
    ``asyncio.Lock`` because the reload is filesystem-bound (sync) and
    we want to coalesce reloads across workers.
    """

    def __init__(
        self,
        app,
        *,
        state_dir: str | Path | None = None,
        env_token: str | None = None,
        dev_mode: bool = False,
    ) -> None:
        super().__init__(app)
        self._dev_mode = dev_mode
        self._state_path = Path(
            state_dir or os.environ.get("ANILA_AGENT_STATE_DIR", DEFAULT_STATE_DIR)
        ) / STATE_FILE_NAME
        self._env_token = env_token if env_token is not None else os.environ.get(
            "CSP_SERVICE_TOKEN", ""
        )

        self._reload_lock = Lock()
        self._token: str = ""
        self._previous_token: str = ""
        self._mode: str = "none"  # "state_file" | "env_legacy" | "none"
        self._reload()

        if self._mode == "none" and not self._dev_mode:
            logger.warning(
                "RotatingServiceTokenMiddleware: no service token configured "
                "(state file %s missing, CSP_SERVICE_TOKEN env unset). All "
                "requests will pass — local dev only.",
                self._state_path,
            )
        elif self._mode == "env_legacy":
            logger.info(
                "RotatingServiceTokenMiddleware: using legacy env-var token "
                "(CSP_SERVICE_TOKEN). Run `anila-core agent bootstrap` to "
                "migrate to per-agent state file at %s.",
                self._state_path,
            )
        elif self._mode == "state_file":
            logger.info(
                "RotatingServiceTokenMiddleware: loaded service token from "
                "state file %s (agent_id=%s).",
                self._state_path,
                self._loaded_meta.get("agent_id"),
            )

    # -- public ---------------------------------------------------------

    @property
    def mode(self) -> str:
        """Useful for `/health` to surface which token source is active."""
        return self._mode

    # -- internal helpers ----------------------------------------------

    _loaded_meta: dict = {}

    def _reload(self) -> None:
        """Re-read the state file or fall back to env var.

        Caller holds ``_reload_lock`` (or is in ``__init__`` where
        single-threaded). Never raises — falls back to the previous
        in-memory value on parse error.
        """
        try:
            if self._state_path.is_file():
                with self._state_path.open("r", encoding="utf-8") as f:
                    payload = json.load(f)
                token = (payload.get("token") or "").strip()
                if token:
                    self._token = token
                    self._previous_token = (
                        payload.get("previous_token") or ""
                    ).strip()
                    self._loaded_meta = payload
                    self._mode = "state_file"
                    return
        except (OSError, json.JSONDecodeError) as exc:
            logger.error(
                "RotatingServiceTokenMiddleware: failed to read state "
                "file %s — %s. Falling back to env var.",
                self._state_path,
                exc,
            )

        # Fall through: state file absent / unreadable / empty.
        env = self._env_token.strip()
        if env:
            self._token = env
            self._previous_token = ""
            self._mode = "env_legacy"
            return

        self._token = ""
        self._previous_token = ""
        self._mode = "none"

    def _matches(self, presented: str) -> bool:
        """Constant-time compare against current AND previous tokens."""
        if not presented:
            return False
        if self._token and hmac.compare_digest(presented, self._token):
            return True
        if self._previous_token and hmac.compare_digest(
            presented, self._previous_token
        ):
            return True
        return False

    # -- ASGI dispatch -------------------------------------------------

    async def dispatch(self, request: Request, call_next):
        if self._dev_mode:
            return await call_next(request)

        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        if self._mode == "none":
            # No source configured: matches the legacy "service_token
            # is None" behaviour — pass through with a warning logged
            # at startup.
            return await call_next(request)

        presented = request.headers.get("X-CSP-Service-Token", "")
        if not presented:
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing X-CSP-Service-Token header"},
            )

        if self._matches(presented):
            return await call_next(request)

        # First-line miss → reload state file once; covers
        # "admin rotated, agent still holds stale in-memory copy".
        with self._reload_lock:
            self._reload()

        if self._matches(presented):
            return await call_next(request)

        return JSONResponse(
            status_code=403,
            content={"detail": "Invalid service token"},
        )


# Back-compat alias for code that still imports ``ApiKeyMiddleware``.
# Keeps the old name resolving to the upgraded constant-time-compare
# version. Will be removed once all callers migrate.
ApiKeyMiddleware = CspServiceTokenMiddleware
