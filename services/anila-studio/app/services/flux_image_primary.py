"""FLUX image-primary fetcher — 執行期向 csp 拉主圖像模型的端點/model,
60 秒 TTL 快取,anila-studio 不重啟即可跟著管理員在 ModelsView 的設定換
FLUX 端點(見 docs/superpowers/specs/2026-07-06-flux-image-primary-design.md
§4)。

結構完全比照 ``services/anila-core-router/main.py`` 的
``_refresh_primary`` / ``_ensure_primary`` 一對:一個 module-level 狀態
dict + ``asyncio.Lock``,鎖內再檢一次 TTL(併發呼叫者共用同一次刷新)。

認證重用 studio 既有的 service-token 機制 —— ``app.config.settings`` 的
``CSP_SERVICE_TOKEN`` 這顆設定值已經在 ``job_reporting.py`` /
``revocation_cache.py`` 用來組 ``X-CSP-Service-Token`` header,這裡沒有
新增任何 env。CSP base URL 同樣沿用 ``settings.CSP_BASE_URL``
(csp_client.py 的既有設定來源)。

錯誤處理(對齊 spec 的錯誤處理表)::

    200                       → 更新快取,回 (endpoint_url, name)
    404 / 409(未設定/已停用)  → 視為「沒有主圖像模型」,回 (None, None),
                                 只在第一次進入這個狀態時 log 一次,
                                 避免每 60 秒刷屏
    連線失敗 / timeout        → 沿用上一次成功快取的值;從未成功過則回
                                 (None, None)
    401 / 403                → 回 (None, None) + log warning(studio 沒有
                                 rotating token 機制,直接 fallback env)

呼叫端(``app.services.studio_render.get_active_flux_provider``)看到
``(None, None)`` 一律 fallback 到 env(FLUX_BACKEND_URL / FLUX_MODEL)。
"""
from __future__ import annotations

import logging
import time
from asyncio import Lock

import httpx

from app.config import is_model_governance_required, settings

logger = logging.getLogger(__name__)

IMAGE_PRIMARY_TTL_SECONDS = 60

# 狀態機:"unknown"(從未成功抓過) → "ok" / "not_found" / "auth_error" /
# "conn_error"。只有 "ok" 與 "conn_error" 會回傳快取的 (endpoint, model)
# ——"conn_error" 沿用上一輪的快取值(可能仍是 None,None,若從未成功過);
# "not_found" / "auth_error" 一律回 (None, None) 逼呼叫端走 env fallback。
_state: dict = {
    "endpoint": None,
    "model": None,
    "status": "unknown",
    "fetched_at": 0.0,
    "logged_not_found": False,
}
_lock = Lock()


def _governance_required() -> bool:
    """Read the deployment posture at call time, so tests/config reloads work."""

    return is_model_governance_required()


def _headers() -> dict[str, str]:
    token = settings.CSP_SERVICE_TOKEN.strip()
    return {"X-CSP-Service-Token": token} if token else {}


def _log_not_found_once() -> None:
    if not _state["logged_not_found"]:
        logger.info(
            "image-primary: csp 尚未設定主圖像模型(或已停用)— "
            "fallback env FLUX_BACKEND_URL/FLUX_MODEL(此訊息只 log 一次)"
        )
        _state["logged_not_found"] = True


async def _refresh_image_primary() -> None:
    """實際打 csp。鎖內再檢一次 TTL,併發呼叫者共用同一次刷新結果。"""
    async with _lock:
        now = time.time()
        if (
            not _governance_required()
            and _state["status"] != "unknown"
            and now - _state["fetched_at"] < IMAGE_PRIMARY_TTL_SECONDS
        ):
            return

        url = f"{settings.CSP_BASE_URL.rstrip('/')}/api/models/image-primary"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url, headers=_headers())
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
        ) as exc:
            logger.warning(
                "image-primary: 連線 csp 失敗(%s)— %s",
                exc,
                (
                    "formal governance 清除快取並拒絕下游"
                    if _governance_required()
                    else "沿用上次快取值"
                ),
            )
            if _governance_required():
                _state["endpoint"] = None
                _state["model"] = None
                _state["status"] = "governance_error"
            else:
                _state["status"] = "conn_error"
            _state["fetched_at"] = now
            return

        if resp.status_code == 200:
            try:
                data = resp.json()
            except (TypeError, ValueError):
                data = None
            endpoint_value = (
                data.get("endpoint_url") if isinstance(data, dict) else None
            )
            model_value = data.get("name") if isinstance(data, dict) else None
            endpoint = (
                endpoint_value.strip() if isinstance(endpoint_value, str) else ""
            )
            model = model_value.strip() if isinstance(model_value, str) else ""
            if endpoint and model:
                _state["endpoint"] = endpoint
                _state["model"] = model
                _state["status"] = "ok"
                _state["logged_not_found"] = False
            else:
                logger.warning(
                    "image-primary: csp 回 200 但缺 endpoint_url — 視同未設定"
                )
                _state["endpoint"] = None
                _state["model"] = None
                _state["status"] = "not_found"
                _log_not_found_once()
        elif resp.status_code in (404, 409):
            # 404 = 未設定;409 = 主圖像模型被停用 —— spec 表格「視同 404
            # fallback」,兩者用同一顆 log-once 開關。
            _state["endpoint"] = None
            _state["model"] = None
            _state["status"] = "not_found"
            _log_not_found_once()
        elif resp.status_code in (401, 403):
            logger.warning(
                "image-primary: csp 拒絕 service token(%s)— "
                "%s",
                resp.status_code,
                "formal governance 清除快取並拒絕下游"
                if _governance_required()
                else "fallback env FLUX_BACKEND_URL/FLUX_MODEL",
            )
            if _governance_required():
                _state["endpoint"] = None
                _state["model"] = None
            _state["status"] = "auth_error"
        else:
            logger.warning(
                "image-primary: csp 回應非預期 status=%s body=%s",
                resp.status_code,
                resp.text[:200] if resp.text else "",
            )
            if _governance_required():
                _state["endpoint"] = None
                _state["model"] = None
            _state["status"] = "conn_error"

        _state["fetched_at"] = now


def _resolve() -> tuple[str | None, str | None]:
    if _state["status"] in ("ok", "conn_error"):
        return _state["endpoint"], _state["model"]
    return None, None


async def get_image_primary() -> tuple[str | None, str | None]:
    """回傳 ``(endpoint_url, model_name)``,60 秒內命中快取不重打 csp。

    兩者皆 ``None`` 時,呼叫端應 fallback 到 env
    (``FLUX_BACKEND_URL`` / ``FLUX_MODEL``)。
    """
    now = time.time()
    if (
        _governance_required()
        or now - _state["fetched_at"] >= IMAGE_PRIMARY_TTL_SECONDS
    ):
        await _refresh_image_primary()
    return _resolve()
