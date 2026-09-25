"""執行期向 CSP 問視覺角色。快取 45 秒。

ingestion-worker 沒有另一把服務憑證：用既有的 VISION_API_KEY
（與嵌入同一把 INTERNAL_PLATFORM_API_KEY）當 Bearer。
該帳號是 system，可以呼叫角色目前指向的任何啟用中模型。

沒設、已停用、或問不到：回 (None, 給人看的訊息)。呼叫端略過圖說，
不讓整份入庫失敗，也不改用寫死的模型名。
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TTL_SECONDS = 45.0
_UNSET = "視覺模型尚未在治理中心設定"
_cache: dict[str, Any] = {"name": None, "message": None, "at": 0.0, "known": False}


def reset_cache() -> None:
    _cache.update(name=None, message=None, at=0.0, known=False)


def csp_origin(vision_url: str) -> str:
    """``http://csp:8000/v1`` → ``http://csp:8000``。"""
    base = (vision_url or "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base.rstrip("/")


async def resolve_vision_model(*, vision_url: str, api_key: str) -> tuple[str | None, str]:
    now = time.monotonic()
    if _cache["known"] and now - float(_cache["at"]) < _TTL_SECONDS:
        if _cache["name"]:
            return _cache["name"], ""
        return None, _cache["message"] or _UNSET

    origin = csp_origin(vision_url)
    if not origin:
        _remember(None, "無法向治理中心確認視覺模型", now)
        return None, _cache["message"]

    url = f"{origin}/api/models/roles/vision"
    headers = {}
    token = (api_key or "").strip()
    if token and token != "not-set":
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=headers)
    except Exception as exc:  # noqa: BLE001 — 問不到就略過圖說
        logger.warning("vision role: 連線 csp 失敗（%s）", exc)
        if _cache["name"]:
            return _cache["name"], ""
        return None, "無法向治理中心確認視覺模型"

    if resp.status_code == 200:
        name = str((resp.json() or {}).get("name") or "").strip()
        if name:
            _remember(name, "", now)
            return name, ""
        _remember(None, _UNSET, now)
        return None, _UNSET

    if resp.status_code in (404, 409):
        message = _UNSET
        try:
            detail = (resp.json() or {}).get("detail")
            if isinstance(detail, str) and detail.strip():
                message = detail.strip()
        except Exception:
            pass
        _remember(None, message, now)
        return None, message

    logger.warning("vision role: csp status=%s", resp.status_code)
    if _cache["name"]:
        return _cache["name"], ""
    return None, "無法向治理中心確認視覺模型"


def cached_vision_model_name() -> str:
    """同步讀 45 秒快取。過期或尚未問過就回空字串，這裡不發 HTTP。"""
    now = time.monotonic()
    if (
        _cache["known"]
        and now - float(_cache["at"]) < _TTL_SECONDS
        and _cache["name"]
    ):
        return str(_cache["name"])
    return ""


async def bind_pdf_ocr_model(*, vision_url: str, api_key: str) -> None:
    """PDF OCR 與圖說共用視覺角色。同步解析器只讀上面的快取。"""
    from anila_core.ingestion.ocr import set_ocr_model_provider

    name, message = await resolve_vision_model(
        vision_url=vision_url, api_key=api_key,
    )
    if not name:
        logger.warning("ingestion-worker: 略過 PDF OCR — %s", message)
    set_ocr_model_provider(cached_vision_model_name)


def _remember(name: str | None, message: str, now: float) -> None:
    _cache["name"] = name
    _cache["message"] = message
    _cache["at"] = now
    _cache["known"] = True
