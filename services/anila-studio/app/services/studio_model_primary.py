"""執行期向 CSP 解析模型角色。成功結果快取 60 秒，治理中心改了不用重啟。

簡報與視覺各問各的角色。沒設、已停用、或指到不相容的模型，就帶著
CSP 回的那句話失敗。不退回環境變數，也不猜模型名稱。

連不上 CSP 時沿用最後一次成功的名稱，但只到 ``ROLE_MAX_STALE_SECONDS``。
失敗只排一個短退避，不把成功時鐘往後推。從來沒成功過，退避一過就再問。
"""
from __future__ import annotations

import logging
import time
from asyncio import Lock

import httpx
from fastapi import HTTPException

from app.config import settings
from app.service_token import headers as service_token_headers
from app.service_token import reload as reload_service_token

logger = logging.getLogger(__name__)

ROLE_TTL_SECONDS = 60
ROLE_RETRY_SECONDS = 5
ROLE_MAX_STALE_SECONDS = 300

# 呼叫端傳這個值，表示「用這個角色」，不是模型名稱，不能送去上游。
SLIDES_ROLE_SENTINEL = "__role:slides__"
VISION_ROLE_SENTINEL = "__role:vision__"
_SENTINEL_ROLE = {
    SLIDES_ROLE_SENTINEL: "slides",
    VISION_ROLE_SENTINEL: "vision",
}

_UNSET = {
    "slides": "簡報模型尚未在治理中心設定",
    "vision": "視覺模型尚未在治理中心設定",
}

# 生圖是可選的。沒設、不健康、或暫時連不上，簡報照出、只是不配生成圖片。
# 不沿用過期的成功值：連不上就當成這次沒有生圖模型。
_IMAGE_ROLE = "image_generation"
_IMAGE_HEALTH_OK = frozenset({"healthy", "online"})
_image_model: str | None = None
_image_checked_at = 0.0


class _Slot:
    def __init__(self) -> None:
        self.model: str | None = None
        self.status = "unknown"
        self.message: str | None = None
        self.succeeded_at = 0.0
        self.retry_after = 0.0


_slots: dict[str, _Slot] = {"slides": _Slot(), "vision": _Slot()}
_lock = Lock()


def _headers() -> dict[str, str]:
    return service_token_headers()


def _detail_from(resp: httpx.Response, role: str) -> str:
    try:
        body = resp.json()
    except Exception:
        body = None
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    return _UNSET[role]


def _success_is_fresh(slot: _Slot, now: float) -> bool:
    return (
        slot.succeeded_at > 0
        and slot.status in ("ok", "unset")
        and now - slot.succeeded_at < ROLE_TTL_SECONDS
    )


def _backing_off(slot: _Slot, now: float) -> bool:
    return slot.retry_after > now


def _stale_model_usable(slot: _Slot, now: float) -> bool:
    return (
        bool(slot.model)
        and slot.succeeded_at > 0
        and now - slot.succeeded_at < ROLE_MAX_STALE_SECONDS
    )


def _should_fetch(slot: _Slot, now: float) -> bool:
    if slot.status == "unknown":
        return True
    if _backing_off(slot, now):
        return False
    if _success_is_fresh(slot, now):
        return False
    return True


def _remember_success(slot: _Slot, now: float) -> None:
    slot.succeeded_at = now
    slot.retry_after = 0.0


def _remember_transient(slot: _Slot, role: str, now: float) -> None:
    """失敗不改成功時鐘。舊名稱過了最長沿用時間就不再給出去。"""
    slot.status = "conn_error"
    slot.retry_after = now + ROLE_RETRY_SECONDS
    if not _stale_model_usable(slot, now):
        slot.message = f"無法向治理中心確認{_UNSET[role][:2]}模型"


async def _refresh(role: str) -> None:
    slot = _slots[role]
    async with _lock:
        now = time.time()
        if not _should_fetch(slot, now):
            return
        url = f"{settings.CSP_BASE_URL.rstrip('/')}/api/models/roles/{role}"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url, headers=_headers())
        except Exception as exc:  # noqa: BLE001 — 短暫連不上就沿用尚未過期的成功值
            logger.warning("model-role %s: 連線 csp 失敗（%s）", role, exc)
            _remember_transient(slot, role, now)
            return
        if resp.status_code in (401, 403):
            reload_service_token(True)
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(url, headers=_headers())
            except Exception as exc:  # noqa: BLE001 — 重試那一次也連不上就沿用舊值
                logger.warning("model-role %s: 重讀憑證後連線 csp 失敗（%s）", role, exc)
                _remember_transient(slot, role, now)
                return
        if resp.status_code == 200:
            name = str((resp.json() or {}).get("name") or "").strip()
            if name:
                if slot.model != name:
                    logger.info("model-role %s = %s", role, name)
                slot.model = name
                slot.status = "ok"
                slot.message = None
            else:
                slot.model = None
                slot.status = "unset"
                slot.message = _UNSET[role]
            _remember_success(slot, now)
            return
        if resp.status_code in (404, 409):
            slot.model = None
            slot.status = "unset"
            slot.message = _detail_from(resp, role)
            logger.info("model-role %s: %s", role, slot.message)
            _remember_success(slot, now)
            return
        logger.warning(
            "model-role %s: csp 回應 status=%s", role, resp.status_code
        )
        _remember_transient(slot, role, now)


async def resolve_image_generation() -> str | None:
    """生圖角色的模型名稱。已設定且健康才回；否則 None，不丟例外。"""
    global _image_model, _image_checked_at
    now = time.time()
    if _image_checked_at and now - _image_checked_at < ROLE_TTL_SECONDS:
        return _image_model
    async with _lock:
        now = time.time()
        if _image_checked_at and now - _image_checked_at < ROLE_TTL_SECONDS:
            return _image_model
        url = f"{settings.CSP_BASE_URL.rstrip('/')}/api/models/roles/{_IMAGE_ROLE}"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url, headers=_headers())
            if resp.status_code in (401, 403):
                reload_service_token(True)
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(url, headers=_headers())
        except Exception as exc:  # noqa: BLE001 — 生圖失敗不擋簡報
            logger.warning("model-role %s: 連線 csp 失敗（%s）", _IMAGE_ROLE, exc)
            _image_model = None
            _image_checked_at = now
            return None
        name: str | None = None
        if resp.status_code == 200:
            body = resp.json() if resp.content else {}
            if isinstance(body, dict):
                raw = str(body.get("name") or "").strip()
                health = str(body.get("health_status") or "").strip()
                if raw and health in _IMAGE_HEALTH_OK:
                    name = raw
        _image_model = name
        _image_checked_at = time.time()
        return name


async def require_role_model(role: str) -> str:
    """角色目前的模型名稱。沒有就 HTTPException，detail 給使用者看。"""
    if role not in _slots:
        raise HTTPException(status_code=500, detail="未知的模型角色")
    slot = _slots[role]
    if _should_fetch(slot, time.time()):
        await _refresh(role)
    now = time.time()
    if (
        slot.model
        and slot.status in ("ok", "conn_error")
        and _stale_model_usable(slot, now)
    ):
        return slot.model
    status = 409 if slot.status == "unset" else 503
    raise HTTPException(status_code=status, detail=slot.message or _UNSET[role])


async def resolve_model_name(requested: str | None, default: str) -> str:
    """明確指定的模型名稱不動。哨兵或與預設相同，就解析對應角色。"""
    if requested and requested not in _SENTINEL_ROLE and requested != default:
        return requested
    role = _SENTINEL_ROLE.get(requested or "") or _SENTINEL_ROLE.get(default) or "slides"
    return await require_role_model(role)


def _reset_for_tests() -> None:
    global _image_model, _image_checked_at
    for slot in _slots.values():
        slot.model = None
        slot.status = "unknown"
        slot.message = None
        slot.succeeded_at = 0.0
        slot.retry_after = 0.0
    _image_model = None
    _image_checked_at = 0.0


def _expire_for_tests() -> None:
    """讓成功快取過期，但留下的名稱仍在最長沿用時間內。"""
    now = time.time()
    for slot in _slots.values():
        if slot.succeeded_at:
            slot.succeeded_at = now - ROLE_TTL_SECONDS - 1
        slot.retry_after = 0.0
