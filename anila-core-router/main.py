"""ANILA Core Router — deployment entrypoint.

Resolves the primary routing LLM from CSP (Models page) rather than a static
env var. Admin picks the model via CSP UI; Router pulls it at boot and every
PRIMARY_TTL_SECONDS.

Usage:
    uvicorn main:app --host 0.0.0.0 --port 9000

Required env:
    CSP_BASE_URL            Base URL of CSP backend (e.g. http://csp:8000)
    CSP_SERVICE_TOKEN       Shared secret for the /api/models/router-primary call

Behavior:
  * Startup: fetch primary from CSP, assign to settings.model so the existing
    router_server.py logic (which reads settings.model when proxying to CSP)
    picks up the admin's choice without any source edits in anila_core.
  * Every /v1/chat/completions request: refresh if TTL expired. If no primary
    is set in CSP, return 503 instead of silently calling a missing model.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import suppress

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

from anila_core.api.router_server import create_router_app
from anila_core.config import settings

logger = logging.getLogger("anila-router")

app = create_router_app()

CSP_BASE_URL = os.environ.get("CSP_BASE_URL", "http://csp:8000").rstrip("/")
CSP_SERVICE_TOKEN = os.environ.get("CSP_SERVICE_TOKEN", "")
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
    async with _primary_lock:
        now = time.time()
        # Re-check after lock: another task may have just refreshed.
        if _primary_state["name"] and now - _primary_state["fetched_at"] < PRIMARY_TTL_SECONDS:
            return
        url = f"{CSP_BASE_URL}/api/models/router-primary"
        headers = {"X-CSP-Service-Token": CSP_SERVICE_TOKEN} if CSP_SERVICE_TOKEN else {}
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
            else:
                _primary_state["name"] = None
                _primary_state["error"] = (
                    f"CSP {resp.status_code}: {resp.text[:200] if resp.text else ''}"
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
    if _primary_state["name"] is None or now - _primary_state["fetched_at"] >= PRIMARY_TTL_SECONDS:
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
    }
