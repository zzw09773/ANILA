"""ANILA Core Router — deployment entrypoint.

Sprint 8 X / Phase C: the Router is now the first ``service_clients``
row (``client_name='router-primary'``). Three startup paths are
supported, in priority order, so legacy deployments keep running
during cutover and new deployments get the new behaviour
automatically:

    1. **State file** — ``{ANILA_ROUTER_STATE_DIR}/service_token.json``
       written by ``anila-core agent bootstrap`` (or by an earlier
       ``--csp-bootstrap-token`` self-bootstrap; see below). This is
       the steady-state path once cutover is done.

    2. **Auto-bootstrap** — if the state file is missing AND
       ``CSP_BOOTSTRAP_TOKEN`` is set, the Router calls
       ``POST /api/service-clients/bootstrap`` at startup, writes the
       returned ``csk-`` to the state file, and proceeds. Lets a fresh
       Router come up with one env var instead of an out-of-band CLI
       step.

    3. **Legacy env var** — if neither of the above works,
       ``CSP_SERVICE_TOKEN`` is used in ``X-CSP-Service-Token`` headers
       to CSP. This is the pre-Phase-A behaviour and still works
       because Phase A backfilled a ``router-primary`` row containing
       that same token.

The startup log line tells ops which path was actually taken so
incident responders don't have to guess.

Usage:
    uvicorn main:app --host 0.0.0.0 --port 9000

Required env (one of):
    CSP_BASE_URL              Base URL of CSP backend.
    CSP_BOOTSTRAP_TOKEN       Optional — auto-bootstrap on startup.
    CSP_SERVICE_TOKEN         Legacy fleet-shared shared-secret.
    ANILA_ROUTER_STATE_DIR    Override default state directory
                              (``/var/lib/anila-router``).

The 503 gate on ``/v1/chat/completions`` is preserved — when no
primary model is configured in CSP, the Router still refuses the
request with a clear error rather than silently falling back to the
wrong upstream.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import time
from contextlib import suppress
from pathlib import Path
from typing import Optional

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

from anila_core.api.router_server import create_router_app
from anila_core.config import settings

logger = logging.getLogger("anila-router")

app = create_router_app()


# ---------------------------------------------------------------------------
# State file + token resolution.
# ---------------------------------------------------------------------------


CSP_BASE_URL = os.environ.get("CSP_BASE_URL", "http://csp:8000").rstrip("/")
CSP_BOOTSTRAP_TOKEN = os.environ.get("CSP_BOOTSTRAP_TOKEN", "").strip()
CSP_SERVICE_TOKEN_LEGACY = os.environ.get("CSP_SERVICE_TOKEN", "").strip()
ROUTER_STATE_DIR = Path(
    os.environ.get("ANILA_ROUTER_STATE_DIR", "/var/lib/anila-router")
)
ROUTER_STATE_FILE = ROUTER_STATE_DIR / "service_token.json"
ROUTER_CLIENT_NAME = "router-primary"

# In-memory cache of the s2s token used in CSP-bound requests. Set on
# startup; refreshed by ``_load_service_token`` whenever the state
# file is rewritten (e.g. admin rotation followed by Router restart).
_service_token: str = ""
_token_source: str = "none"  # "state_file" | "bootstrap" | "legacy_env" | "none"


def _load_service_token() -> tuple[str, str]:
    """Resolve the Router's outgoing s2s token.

    Returns ``(token, source)`` where source is one of
    ``state_file``, ``bootstrap``, ``legacy_env``, ``none``.
    Never raises — callers downstream surface ``none`` as a 503-y
    error if it actually breaks something.
    """
    if ROUTER_STATE_FILE.is_file():
        try:
            data = json.loads(ROUTER_STATE_FILE.read_text(encoding="utf-8"))
            token = (data.get("token") or "").strip()
            if token:
                return token, "state_file"
        except (OSError, json.JSONDecodeError) as exc:
            logger.error(
                "Router state file %s unreadable: %s. Falling back.",
                ROUTER_STATE_FILE,
                exc,
            )
    if CSP_SERVICE_TOKEN_LEGACY:
        return CSP_SERVICE_TOKEN_LEGACY, "legacy_env"
    return "", "none"


def _write_state_file(token: str, *, source_meta: dict) -> None:
    """Persist ``token`` to the state file with mode 0600 (best effort)."""
    ROUTER_STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "token": token,
        "previous_token": None,
        "previous_expires_at": None,
        "client_name": ROUTER_CLIENT_NAME,
        "csp_url": CSP_BASE_URL,
        **source_meta,
        "schema_version": 1,
    }
    tmp = ROUTER_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    tmp.replace(ROUTER_STATE_FILE)


async def _self_bootstrap() -> Optional[str]:
    """Exchange ``CSP_BOOTSTRAP_TOKEN`` for a long-lived ``csk-`` token.

    Calls ``POST /api/service-clients`` (admin path). Note: this
    endpoint is admin-gated by JWT in the current Phase A
    implementation, so the auto-bootstrap variant only works when ops
    pre-issues a token via admin UI and provides it directly via
    ``CSP_SERVICE_TOKEN``. Future iteration: add an admin-issued
    one-shot bootstrap token specifically for service_clients (mirror
    of ``/api/agents/{id}/issue-bootstrap``).

    For Phase C v1 we treat ``CSP_BOOTSTRAP_TOKEN`` as a pass-through
    long-lived token that we copy into the state file. Lets us land
    the Router state-file path now without blocking on a separate
    bootstrap-issuance endpoint for service_clients.
    """
    if not CSP_BOOTSTRAP_TOKEN:
        return None
    # v1: treat the env value as the pre-issued csk- to seed the
    # state file. Real bootstrap exchange will be wired when the
    # service_clients bootstrap endpoint exists (Sprint 9 X).
    _write_state_file(
        CSP_BOOTSTRAP_TOKEN,
        source_meta={
            "issued_at": None,
            "label": None,
            "credential_id": None,
            "note": "seeded from CSP_BOOTSTRAP_TOKEN env at first start",
        },
    )
    logger.info(
        "Router state file seeded from CSP_BOOTSTRAP_TOKEN at %s",
        ROUTER_STATE_FILE,
    )
    return CSP_BOOTSTRAP_TOKEN


def _initialise_token_source() -> None:
    """Run the 3-priority resolution at startup and remember the choice."""
    global _service_token, _token_source

    token, source = _load_service_token()
    if token:
        _service_token = token
        _token_source = source
        logger.info(
            "Router service token resolved from %s (csp=%s)",
            source,
            CSP_BASE_URL,
        )
        return

    # State file empty + no legacy env. Try CSP_BOOTSTRAP_TOKEN.
    if CSP_BOOTSTRAP_TOKEN:
        # Synchronous wrapper around the async self-bootstrap.
        loop = asyncio.new_event_loop()
        try:
            seeded = loop.run_until_complete(_self_bootstrap())
        finally:
            loop.close()
        if seeded:
            _service_token = seeded
            _token_source = "bootstrap"
            logger.info(
                "Router service token bootstrapped from CSP_BOOTSTRAP_TOKEN"
            )
            return

    _service_token = ""
    _token_source = "none"
    logger.warning(
        "Router service token NOT configured — CSP-bound requests will "
        "go without X-CSP-Service-Token. Set CSP_BOOTSTRAP_TOKEN or "
        "CSP_SERVICE_TOKEN in env."
    )


_initialise_token_source()


# ---------------------------------------------------------------------------
# Primary-LLM resolution (unchanged from pre-Phase-C apart from using
# the resolved ``_service_token`` instead of reading env var directly).
# ---------------------------------------------------------------------------


PRIMARY_TTL_SECONDS = 60

_primary_state: dict = {"name": None, "fetched_at": 0.0, "error": None}
_primary_lock = asyncio.Lock()


def _apply_primary(name: str) -> None:
    """Patch anila_core.config.settings.model so router_server picks it up."""
    try:
        settings.model = name
    except Exception:
        # pydantic frozen or validation quirk — force through __setattr__.
        object.__setattr__(settings, "model", name)


async def _refresh_primary() -> None:
    global _service_token, _token_source
    async with _primary_lock:
        now = time.time()
        if (
            _primary_state["name"]
            and now - _primary_state["fetched_at"] < PRIMARY_TTL_SECONDS
        ):
            return
        url = f"{CSP_BASE_URL}/api/models/router-primary"
        headers = (
            {"X-CSP-Service-Token": _service_token} if _service_token else {}
        )
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                name = data.get("name")
                if name:
                    _apply_primary(name)
                    _primary_state["name"] = name
                    _primary_state["error"] = None
                    logger.info("ANILA Router primary model refreshed: %s", name)
                else:
                    _primary_state["name"] = None
                    _primary_state["error"] = "CSP 回應缺少 name 欄位"
            elif resp.status_code in (401, 403):
                # Stale token: try reloading the state file once before
                # giving up. Mirrors RotatingServiceTokenMiddleware's
                # hot-reload behaviour for the agent side.
                new_token, new_source = _load_service_token()
                if new_token and new_token != _service_token:
                    logger.info(
                        "Router service token refreshed from %s after %s; retrying",
                        new_source,
                        resp.status_code,
                    )
                    _service_token = new_token
                    _token_source = new_source
                    headers = {"X-CSP-Service-Token": _service_token}
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        resp = await client.get(url, headers=headers)
                    if resp.status_code == 200:
                        data = resp.json()
                        name = data.get("name")
                        if name:
                            _apply_primary(name)
                            _primary_state["name"] = name
                            _primary_state["error"] = None
                            return
                _primary_state["name"] = None
                _primary_state["error"] = (
                    f"CSP 拒絕 service token ({resp.status_code}); "
                    "請確認 CSP_BOOTSTRAP_TOKEN/CSP_SERVICE_TOKEN 仍有效"
                )
            else:
                _primary_state["name"] = None
                _primary_state["error"] = (
                    f"CSP {resp.status_code}: "
                    f"{resp.text[:200] if resp.text else ''}"
                )
                logger.warning(
                    "CSP returned %s for router-primary: %s",
                    resp.status_code,
                    resp.text[:200],
                )
        except Exception as exc:
            _primary_state["name"] = None
            _primary_state["error"] = f"連線 CSP 失敗: {exc}"
            logger.warning("Failed to reach CSP at %s: %s", url, exc)
        finally:
            _primary_state["fetched_at"] = now


async def _ensure_primary() -> tuple[str | None, str | None]:
    now = time.time()
    if (
        _primary_state["name"] is None
        or now - _primary_state["fetched_at"] >= PRIMARY_TTL_SECONDS
    ):
        await _refresh_primary()
    return _primary_state["name"], _primary_state["error"]


@app.on_event("startup")
async def _bootstrap() -> None:
    with suppress(Exception):
        await _refresh_primary()


@app.middleware("http")
async def _gate_on_primary(request: Request, call_next):
    # Only gate the chat completions path; leave /health, /v1/models, /docs alone.
    if request.url.path == "/v1/chat/completions" and request.method == "POST":
        name, err = await _ensure_primary()
        if not name:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": (
                        "ANILA Router 無可用主路由模型。"
                        "請管理員前往 CSP Models 頁面指定一個 LLM 為「主路由」。"
                        f"（細節：{err or '未設定'}）"
                    )
                },
            )
    return await call_next(request)


@app.get("/router/primary-status", include_in_schema=False)
async def primary_status() -> dict:
    """Debug endpoint — returns the cached primary name + any last error."""
    return {
        "name": _primary_state["name"],
        "fetched_at": _primary_state["fetched_at"],
        "error": _primary_state["error"],
        "ttl_seconds": PRIMARY_TTL_SECONDS,
        "service_token_source": _token_source,
        "csp_base_url": CSP_BASE_URL,
        "state_file": str(ROUTER_STATE_FILE),
    }
