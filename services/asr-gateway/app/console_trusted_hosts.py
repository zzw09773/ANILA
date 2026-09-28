"""治理中心的信任主機。asr-gateway 的環境清單是空的，改問 CSP。

快取約 30 秒。這次讀不到就沿用上一筆，但超過五分鐘仍讀不到就清空，
不讓已刪除的主機一直留在 SSRF 放行清單。從來沒讀到就是空的，不猜主機。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx

from anila_core.security.url_guard import register_trusted_host_provider

logger = logging.getLogger(__name__)

_PATH = "/api/internal/trusted-hosts"
_TTL_SECONDS = 30.0
_MAX_STALE_SECONDS = 300.0
_lock = threading.Lock()
_hosts: set[str] = set()
_expires_at = 0.0
_fetched_at = 0.0
_registered = False


def cached_console_hosts() -> set[str]:
    with _lock:
        return set(_hosts)


def reset_console_trusted_hosts() -> None:
    """測試用。清掉快取，不拿掉已經掛上的 provider。"""
    global _expires_at, _fetched_at
    with _lock:
        _hosts.clear()
        _expires_at = 0.0
        _fetched_at = 0.0


def ensure_registered() -> None:
    global _registered
    if _registered:
        return
    register_trusted_host_provider(cached_console_hosts)
    _registered = True


ensure_registered()


def _drop_stale_hosts(now: float) -> None:
    """讀取失敗時，上一筆只再留五分鐘。"""
    global _fetched_at
    if _fetched_at and now - _fetched_at > _MAX_STALE_SECONDS:
        _hosts.clear()
        _fetched_at = 0.0


async def refresh_console_trusted_hosts(
    settings: Any,
    *,
    http_client: httpx.AsyncClient | None = None,
    force: bool = False,
) -> None:
    """用語音服務權杖讀信任主機。短暫失敗留著上一筆，太久就清空。"""
    global _expires_at, _fetched_at
    ensure_registered()
    now = time.monotonic()
    with _lock:
        if not force and now < _expires_at:
            return
    from app.decode_endpoint import _service_token

    token = _service_token(settings)
    base = str(getattr(settings, "CSP_BASE_URL", "") or "").rstrip("/")
    if not token or not base:
        with _lock:
            _drop_stale_hosts(time.monotonic())
        return
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient()
    try:
        response = await client.get(
            f"{base}{_PATH}",
            headers={"X-CSP-Service-Token": token},
            timeout=5.0,
        )
        if response.status_code != 200:
            logger.warning("trusted hosts refresh HTTP %s", response.status_code)
            with _lock:
                _drop_stale_hosts(time.monotonic())
            return
        payload = response.json() or {}
        fresh = {
            str(host).strip().lower()
            for host in (payload.get("hosts") or [])
            if str(host).strip()
        }
    except Exception:
        logger.warning("trusted hosts refresh failed", exc_info=True)
        with _lock:
            _drop_stale_hosts(time.monotonic())
        return
    finally:
        if owns_client:
            await client.aclose()
    with _lock:
        _hosts.clear()
        _hosts.update(fresh)
        moment = time.monotonic()
        _fetched_at = moment
        _expires_at = moment + _TTL_SECONDS
