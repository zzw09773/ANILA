"""ANILA Core Router — deployment entrypoint.

The Router's outgoing CSP credential is a per-client token that CSP
provisions itself. Nobody copies it into an env file. Resolution order:

    1. **Token file** — ``ANILA_SERVICE_TOKEN_FILE``
       (compose: ``/run/anila/service-clients/router-primary.token``,
       written by CSP, mode 0640). Re-read when the file changes, on a
       timer, and once after CSP returns 401 or 403.
       If this path is configured, no other credential is used.
       A missing file is ``file_missing``; an unreadable or empty file
       is ``file_error``. Both keep being re-read so a later CSP write
       is picked up. The plaintext is never logged.

    2. **State file** — ``{ANILA_ROUTER_STATE_DIR}/service_token.json``.
       Fallback only when ``ANILA_SERVICE_TOKEN_FILE`` is unset.

    3. **CSP_BOOTSTRAP_TOKEN** — legacy escape hatch, used only when the
       token file is unset and the state file is empty. The value is
       copied into the state file. This does not call CSP.

    4. **CSP_SERVICE_TOKEN** — the old fleet-wide secret, used only when
       the token file is unset and the earlier fallbacks are empty.
       Router-only CSP endpoints reject it once ``router-primary`` has
       its own credential (403 ``legacy_env_cannot_satisfy_client_type``).

Startup logs the source name (``file``, ``file_missing``, ``file_error``,
``state_file``, ``bootstrap``, ``legacy_env``, or ``none``) and
``/health`` reports it as ``token_source``. The plaintext is never logged.

Usage:
    uvicorn main:app --host 0.0.0.0 --port 9000

The 503 gate on ``/v1/chat/completions`` is preserved — when no
primary model is configured in CSP, the Router refuses the request
instead of silently using a different upstream.
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

from anila_core.api import router_server as _router_server
from anila_core.api.routing import routed_path
from anila_core.config import settings

logger = logging.getLogger("anila-router")

app = _router_server.create_router_app()


# ---------------------------------------------------------------------------
# State file + token resolution.
# ---------------------------------------------------------------------------


CSP_BASE_URL = os.environ.get("CSP_BASE_URL", "http://csp:8000").rstrip("/")
ROUTER_STATE_DIR = Path(
    os.environ.get("ANILA_ROUTER_STATE_DIR", "/var/lib/anila-router")
)
ROUTER_STATE_FILE = ROUTER_STATE_DIR / "service_token.json"
ROUTER_CLIENT_NAME = "router-primary"

# In-memory cache of the s2s token used in CSP-bound requests.
# ``file`` is the CSP-provisioned credential. The other names are fallbacks.
_service_token: str = ""
# file | file_missing | file_error | state_file | bootstrap | legacy_env | none
_token_source: str = "none"
_file_mtime_ns: int | None = None
_token_watch_task: asyncio.Task | None = None


def _service_token_file_path() -> Optional[Path]:
    raw = os.environ.get("ANILA_SERVICE_TOKEN_FILE", "").strip()
    return Path(raw) if raw else None


def _bootstrap_env_token() -> str:
    return os.environ.get("CSP_BOOTSTRAP_TOKEN", "").strip()


def _legacy_env_token() -> str:
    return os.environ.get("CSP_SERVICE_TOKEN", "").strip()


def _token_file_disposition(path: Path) -> str:
    """``missing`` | ``ready`` | ``error``.

    Fallback credentials are allowed only for ``missing``. A stat error
    is not "missing": the file may exist and be unreadable.
    """
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        logger.error(
            "Router token file cannot be stat'ed (%s); not using fallback credentials",
            exc.__class__.__name__,
        )
        return "error"
    if not stat.S_ISREG(mode):
        logger.error(
            "Router token file is not a regular file; not using fallback credentials"
        )
        return "error"
    return "ready"


def _load_service_token() -> tuple[str, str]:
    """Resolve the Router's outgoing s2s token.

    Returns ``(token, source)``. Source is ``file``, ``file_missing``,
    ``file_error``, ``state_file``, ``bootstrap``, ``legacy_env``, or
    ``none``. Never raises and never logs the token. When
    ``ANILA_SERVICE_TOKEN_FILE`` is set, a missing file is
    ``file_missing`` and an unreadable or empty file is ``file_error``.
    Neither falls through to another credential. State file, bootstrap,
    and the legacy env secret are used only when that path is unset.
    """
    token_file = _service_token_file_path()
    if token_file is not None:
        disposition = _token_file_disposition(token_file)
        if disposition == "missing":
            logger.error(
                "Router token file is missing; not using fallback credentials"
            )
            return "", "file_missing"
        if disposition == "error":
            return "", "file_error"
        if disposition == "ready":
            try:
                token = token_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                logger.error(
                    "Router token file is unreadable (%s); not using fallback credentials",
                    exc.__class__.__name__,
                )
                return "", "file_error"
            if not token:
                logger.error(
                    "Router token file is empty; not using fallback credentials"
                )
                return "", "file_error"
            return token, "file"
        return "", "file_error"
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
    bootstrap = _bootstrap_env_token()
    if bootstrap:
        return bootstrap, "bootstrap"
    legacy = _legacy_env_token()
    if legacy:
        return legacy, "legacy_env"
    return "", "none"


def _remember_file_mtime() -> None:
    global _file_mtime_ns
    path = _service_token_file_path()
    if path is None or not path.is_file():
        _file_mtime_ns = None
        return
    try:
        _file_mtime_ns = path.stat().st_mtime_ns
    except OSError:
        _file_mtime_ns = None


def _publish_service_token(token: str, source: str) -> None:
    """Install ``token`` for both this module and anila-core's CSP calls."""
    global _service_token, _token_source
    _service_token = token
    _token_source = source
    settings.csp_service_token = token or None
    _remember_file_mtime()


def _reload_service_token(force: bool = False) -> None:
    """Re-read the credential file when it changed, or when ``force`` is set.

    ``file_error`` and ``file_missing`` are always re-read. chmod does
    not bump mtime, and a file CSP has not created yet has no mtime to
    compare, so either failure would otherwise stay stuck.
    """
    path = _service_token_file_path()
    if not force and path is not None and _token_source not in {
        "file_error",
        "file_missing",
    }:
        try:
            if path.is_file() and path.stat().st_mtime_ns == _file_mtime_ns:
                return
        except OSError as exc:
            logger.error(
                "Router token file stat failed (%s)",
                exc.__class__.__name__,
            )
    token, source = _load_service_token()
    if token == _service_token and source == _token_source:
        _remember_file_mtime()
        return
    _publish_service_token(token, source)
    logger.info("Router service token reloaded from %s", source)


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


def _seed_state_from_bootstrap(token: str) -> None:
    """Copy the legacy bootstrap env value into the state file once.

    This is not an HTTP exchange with CSP. It runs only when
    ``ANILA_SERVICE_TOKEN_FILE`` is unset and the state file is empty.
    """
    if ROUTER_STATE_FILE.is_file():
        return
    _write_state_file(
        token,
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


def _initialise_token_source() -> None:
    """Resolve the credential and remember which source won."""
    token, source = _load_service_token()
    if source == "bootstrap" and token:
        _seed_state_from_bootstrap(token)
    _publish_service_token(token, source)
    if source in {"file_error", "file_missing"}:
        logger.error(
            "Router service token source=%s csp=%s. "
            "Configured token file is not usable; fallback credentials are not used.",
            source,
            CSP_BASE_URL,
        )
        return
    if token:
        logger.info(
            "Router service token source=%s csp=%s",
            source,
            CSP_BASE_URL,
        )
        return
    logger.warning(
        "Router service token source=none csp=%s. Expected "
        "ANILA_SERVICE_TOKEN_FILE to be provisioned by CSP. State file, "
        "CSP_BOOTSTRAP_TOKEN, and CSP_SERVICE_TOKEN are used only when "
        "that path is unset.",
        CSP_BASE_URL,
    )


def _router_service_token_source() -> str:
    return _token_source


_router_server.register_service_token_reloader(_reload_service_token)
_router_server.router_service_token_source = _router_service_token_source
_initialise_token_source()


# ---------------------------------------------------------------------------
# Primary-LLM resolution (unchanged from pre-Phase-C apart from using
# the resolved ``_service_token`` instead of reading env var directly).
# ---------------------------------------------------------------------------


PRIMARY_TTL_SECONDS = 60

_primary_state: dict = {"name": None, "fetched_at": 0.0, "error": None}
_primary_lock = asyncio.Lock()


def _apply_primary(name: str) -> None:
    """Record the campus default for fallback only. Never mutate shared settings.model."""
    _primary_state["name"] = name


async def _refresh_primary() -> None:
    async with _primary_lock:
        _reload_service_token(False)
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
                # Stale token: re-read the credential file once before
                # giving up. A rotation that landed during this request
                # is picked up here; a revoked client stays absent.
                previous = _service_token
                _reload_service_token(True)
                if _service_token and _service_token != previous:
                    logger.info(
                        "Router service token refreshed from %s after %s; retrying",
                        _token_source,
                        resp.status_code,
                    )
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
                    "請確認憑證檔仍由 CSP 核發且客戶端未被吊銷"
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


def _token_reload_interval() -> float:
    raw = os.environ.get("ANILA_SERVICE_TOKEN_RELOAD_SECONDS", "30").strip()
    try:
        value = float(raw)
    except ValueError:
        value = 30.0
    return max(5.0, value)


async def _watch_service_token_file() -> None:
    while True:
        await asyncio.sleep(_token_reload_interval())
        try:
            _reload_service_token(False)
        except Exception:
            logger.exception("service token file watch failed")


@app.on_event("startup")
async def _bootstrap() -> None:
    global _token_watch_task
    if _token_watch_task is None or _token_watch_task.done():
        _token_watch_task = asyncio.create_task(_watch_service_token_file())
    with suppress(Exception):
        await _refresh_primary()


@app.on_event("shutdown")
async def _stop_token_watch() -> None:
    global _token_watch_task
    task = _token_watch_task
    _token_watch_task = None
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _request_has_explicit_router_selection(request: Request) -> bool:
    header = (request.headers.get("X-ANILA-Router-Model") or "").strip()
    if header and header != "anila-router":
        return True
    conv = (request.headers.get("X-ANILA-Conversation-Id") or "").strip()
    if conv:
        return True
    try:
        raw = await request.body()
    except Exception:
        return False
    if not raw:
        return False
    try:
        parsed = json.loads(raw)
    except Exception:
        return False
    if not isinstance(parsed, dict):
        return False
    body_model = parsed.get("router_model")
    if isinstance(body_model, str):
        body_model = body_model.strip()
    return bool(body_model) and body_model != "anila-router"


@app.middleware("http")
async def _gate_on_primary(request: Request, call_next):
    # Only gate the chat completions path; leave /health and /v1/models alone.
    # Docs/openapi are disabled at create_router_app (P2.3) — not exempted here.
    # ``routed_path`` rather than ``request.url.path``: the latter is built
    # from the caller's ``Host`` header, so ``Host: x/v1`` makes this
    # comparison miss and the request goes through ungated while the router
    # still dispatches it to the chat completions endpoint.
    if routed_path(request) == "/v1/chat/completions" and request.method == "POST":
        if await _request_has_explicit_router_selection(request):
            return await call_next(request)
        name, err = await _ensure_primary()
        if not name:
            logger.warning(
                "Primary router unavailable; rejecting chat completion: %s",
                err or "未設定",
            )
            return JSONResponse(
                status_code=503,
                content={
                    "detail": (
                        "ANILA Router 無可用主路由模型。"
                        "請管理員前往 CSP Models 頁面指定一個 LLM 為「主路由」。"
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
