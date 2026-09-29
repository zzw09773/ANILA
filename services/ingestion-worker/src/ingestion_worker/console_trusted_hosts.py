"""治理中心的信任主機。ingestion-worker 不再靠環境變數點名私網主機。

每個工作開始前跟文件解析設定一起讀一次（約 30 秒內重複呼叫不再打 CSP）。
這次讀不到就沿用上一筆，但超過五分鐘仍讀不到就清空，不讓已刪除的主機
一直留在 SSRF 放行清單。從來沒讀到就是空的，不猜主機。
與 asr-gateway 的 ``console_trusted_hosts`` 同一套規則。
"""
from __future__ import annotations

import logging
import threading
import time

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


def reset_for_tests() -> None:
    """清掉快取，不拿掉已經掛上的 provider。"""
    global _expires_at, _fetched_at
    with _lock:
        _hosts.clear()
        _expires_at = 0.0
        _fetched_at = 0.0


def ensure_registered() -> None:
    global _registered
    with _lock:
        if _registered:
            return
        register_trusted_host_provider(cached_console_hosts)
        _registered = True


def _drop_stale_hosts(now: float) -> None:
    global _fetched_at
    if _fetched_at and now - _fetched_at > _MAX_STALE_SECONDS:
        _hosts.clear()
        _fetched_at = 0.0


async def refresh_console_trusted_hosts(
    csp_base: str,
    token: str,
    *,
    http_client: httpx.AsyncClient | None = None,
    force: bool = False,
) -> None:
    """用讀文件解析的 sk- 讀信任主機。短暫失敗留著上一筆，太久就清空。"""
    global _expires_at, _fetched_at
    ensure_registered()
    with _lock:
        if not force and time.monotonic() < _expires_at:
            return
    base = (csp_base or "").rstrip("/")
    if not token or not base:
        with _lock:
            _drop_stale_hosts(time.monotonic())
        return
    try:
        if http_client is not None:
            response = await http_client.get(
                f"{base}{_PATH}", headers={"Authorization": f"Bearer {token}"}
            )
        else:
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
                response = await client.get(
                    f"{base}{_PATH}", headers={"Authorization": f"Bearer {token}"}
                )
        if response.status_code != 200:
            logger.warning("ingestion-worker: 信任主機讀取 HTTP %s", response.status_code)
            with _lock:
                _drop_stale_hosts(time.monotonic())
            return
        payload = response.json()
        hosts = payload.get("hosts") if isinstance(payload, dict) else None
        if not isinstance(hosts, list):
            # 形狀不對不是「清單變空」：當成這次讀不到，沿用上一筆。
            logger.warning("ingestion-worker: 信任主機回應格式不對")
            with _lock:
                _drop_stale_hosts(time.monotonic())
            return
        fresh = {str(host).strip().lower() for host in hosts if str(host).strip()}
    except Exception:
        logger.warning("ingestion-worker: 信任主機讀取失敗", exc_info=True)
        with _lock:
            _drop_stale_hosts(time.monotonic())
        return
    with _lock:
        _hosts.clear()
        _hosts.update(fresh)
        moment = time.monotonic()
        _fetched_at = moment
        _expires_at = moment + _TTL_SECONDS
