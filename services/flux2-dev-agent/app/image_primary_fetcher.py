"""Fetch CSP's registered "image primary" FLUX endpoint/model at
request time, with a 60s TTL cache + ``asyncio.Lock`` so concurrent
generation requests don't stampede CSP.

Structure mirrors ``services/anila-core-router/main.py``'s
``_refresh_primary`` (same TTL-gated refresh, same
"X-CSP-Service-Token" header). See
``docs/specs/specs/2026-07-06-flux-image-primary-design.md``
§3 and its 錯誤處理表 for the exact behaviour this implements:

    csp 200             -> cache (endpoint_url, name); authoritative,
                           overrides whatever was cached before.
    csp 404 / 409        -> primary explicitly unset/disabled by an
                           admin — honour that and drop any stale
                           cached value so the caller falls back to
                           env config instead of serving a stale
                           primary.
    csp 401 / 403        -> service token rejected. flux2-dev-agent has
                           no rotating-token mechanism (unlike
                           anila-core-router), so per the spec's error
                           table the cached value is dropped and the
                           caller falls back to env config; a revoked
                           token must not keep serving the last CSP
                           value forever.
    connection error /
    timeout / other 5xx  -> transient failure; previous cache (if any)
                           is left untouched.

Every attempt (success or failure) stamps ``_fetched_at`` so the next
attempt only happens after the TTL elapses — this is what stops a
permanently-404 deployment from re-hitting CSP on every single image
generation request. Logging is deduped by "state" so a steady-state
condition (e.g. permanently unset) logs once, not once per TTL cycle
(spec: 別每 60 秒刷屏).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 60.0


class ImagePrimaryFetcher:
    def __init__(
        self,
        *,
        csp_base_url: str,
        service_token: str,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        timeout: float = 5.0,
    ) -> None:
        self._csp_base_url = csp_base_url.rstrip("/")
        self._service_token = service_token
        self._ttl_seconds = ttl_seconds
        self._timeout = timeout
        self._lock = asyncio.Lock()

        self._endpoint: Optional[str] = None
        self._model: Optional[str] = None
        self._fetched_at: float = 0.0
        self._last_logged_state: Optional[str] = None

    async def get(self) -> tuple[Optional[str], Optional[str]]:
        """Return the cached/fresh ``(endpoint_url, model_name)``.

        ``(None, None)`` means "CSP has no usable image-primary right
        now" (never fetched, explicitly unset, or rejected with no
        prior cache) — the caller is responsible for falling back to
        env config.
        """
        async with self._lock:
            now = time.monotonic()
            if self._fetched_at and (now - self._fetched_at) < self._ttl_seconds:
                return self._endpoint, self._model
            await self._refresh(now)
            return self._endpoint, self._model

    def _log_once(self, state: str, message: str, *, level: int = logging.WARNING) -> None:
        if state == self._last_logged_state:
            return
        self._last_logged_state = state
        logger.log(level, message)

    async def _refresh(self, now: float) -> None:
        url = f"{self._csp_base_url}/api/models/image-primary"
        headers = (
            {"X-CSP-Service-Token": self._service_token} if self._service_token else {}
        )
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(url, headers=headers)
        except Exception as exc:
            self._fetched_at = now
            self._log_once(
                "conn_error",
                "image-primary fetch 失敗，沿用快取值 "
                f"(endpoint={self._endpoint!r}, model={self._model!r}): {exc}",
            )
            return

        self._fetched_at = now

        if resp.status_code == 200:
            try:
                data = resp.json()
            except Exception:
                self._log_once(
                    "malformed",
                    f"CSP image-primary 回應非 JSON: {resp.text[:200]}",
                )
                return
            endpoint = data.get("endpoint_url")
            name = data.get("name")
            if endpoint and name:
                self._endpoint = endpoint
                self._model = name
                self._last_logged_state = None  # reset dedupe on recovery
                return
            self._log_once(
                "malformed",
                f"CSP image-primary 回應缺少 endpoint_url/name: {str(data)[:200]}",
            )
            return

        if resp.status_code in (404, 409):
            # Admin explicitly hasn't set (404) or has disabled (409) an
            # image primary — honour that and drop any stale cached
            # value so the caller falls back to env.
            self._endpoint = None
            self._model = None
            self._log_once(
                "unset",
                f"CSP 未設定或已停用主圖像模型（{resp.status_code}），改用 env fallback",
                level=logging.INFO,
            )
            return

        if resp.status_code in (401, 403):
            # spec 錯誤處理表：無 rotating token 機制 → fallback env。
            # 必須清掉舊快取，否則 token 被撤銷後會永遠沿用最後一次的 CSP 值。
            self._endpoint = None
            self._model = None
            self._log_once(
                "auth_failed",
                f"CSP service token 被拒（{resp.status_code}）；"
                "flux2-dev-agent 無 rotating token 機制，改用 env fallback",
            )
            return

        self._log_once(
            f"http_{resp.status_code}",
            f"CSP image-primary 回應非預期狀態碼 {resp.status_code}: {resp.text[:200]}",
        )
