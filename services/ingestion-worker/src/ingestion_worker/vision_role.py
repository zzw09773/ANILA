"""執行期向 CSP 問視覺角色。快取 45 秒。

ingestion-worker 沒有另一把服務憑證：用既有的系統 API key 檔當 Bearer。
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
_caches: dict[str, dict[str, Any]] = {}


def _slot(role: str) -> dict[str, Any]:
    slot = _caches.get(role)
    if slot is None:
        slot = {"name": None, "message": None, "at": 0.0, "known": False}
        _caches[role] = slot
    return slot


def reset_cache() -> None:
    _caches.clear()


def csp_origin(vision_url: str) -> str:
    """``http://csp:8000/v1`` → ``http://csp:8000``。"""
    base = (vision_url or "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base.rstrip("/")


async def resolve_role_model(
    role: str,
    *,
    vision_url: str,
    api_key: str,
    unset_message: str,
) -> tuple[str | None, str]:
    """問治理中心 ``GET /api/models/roles/{role}``。沒設就回 (None, 訊息)。"""
    slot = _slot(role)
    now = time.monotonic()
    if slot["known"] and now - float(slot["at"]) < _TTL_SECONDS:
        if slot["name"]:
            return slot["name"], ""
        return None, slot["message"] or unset_message

    origin = csp_origin(vision_url)
    if not origin:
        _remember(role, None, f"無法向治理中心確認{role}模型", now)
        return None, slot["message"]

    url = f"{origin}/api/models/roles/{role}"
    from ingestion_worker.credential_file import api_key as credential_api_key
    from ingestion_worker.credential_file import reload as reload_credential

    def _headers(presented: str) -> dict[str, str]:
        token = credential_api_key(presented).strip()
        if token and token != "not-set":
            return {"Authorization": f"Bearer {token}"}
        return {}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=_headers(api_key))
            if resp.status_code in (401, 403):
                reload_credential(True)
                resp = await client.get(url, headers=_headers(api_key))
    except Exception as exc:  # noqa: BLE001 — 問不到就略過，不猜模型名
        logger.warning("%s role: 連線 csp 失敗（%s）", role, exc)
        if slot["name"]:
            return slot["name"], ""
        return None, f"無法向治理中心確認{role}模型"

    if resp.status_code == 200:
        name = str((resp.json() or {}).get("name") or "").strip()
        if name:
            _remember(role, name, "", now)
            return name, ""
        _remember(role, None, unset_message, now)
        return None, unset_message

    if resp.status_code in (404, 409):
        message = unset_message
        try:
            detail = (resp.json() or {}).get("detail")
            if isinstance(detail, str) and detail.strip():
                message = detail.strip()
        except Exception:
            pass
        _remember(role, None, message, now)
        return None, message

    logger.warning("%s role: csp status=%s", role, resp.status_code)
    if slot["name"]:
        return slot["name"], ""
    return None, f"無法向治理中心確認{role}模型"


async def resolve_vision_model(*, vision_url: str, api_key: str) -> tuple[str | None, str]:
    return await resolve_role_model(
        "vision",
        vision_url=vision_url,
        api_key=api_key,
        unset_message=_UNSET,
    )


def cached_vision_model_name() -> str:
    """同步讀 45 秒快取。過期或尚未問過就回空字串，這裡不發 HTTP。"""
    slot = _caches.get("vision")
    if slot is None:
        return ""
    now = time.monotonic()
    if (
        slot["known"]
        and now - float(slot["at"]) < _TTL_SECONDS
        and slot["name"]
    ):
        return str(slot["name"])
    return ""


def _remember(role: str, name: str | None, message: str, now: float) -> None:
    slot = _slot(role)
    slot["name"] = name
    slot["message"] = message
    slot["at"] = now
    slot["known"] = True
