"""主簡報模型 fetcher — 執行期向 csp 問 GET /api/models/slides-primary，60 秒 TTL。

2026-09-02：之前模型名寫死在 ``studio_config.SLIDES_LLM_MODEL``（預設 gemma4），
compose 又沒把環境變數接進來，Studio 在這台機器上從沒成功跑過。現在管理員在
模型頁點「設為主簡報」，studio 下一次呼叫就換過去，不用重啟。

結構比照 ``flux_image_primary.py``：module-level 狀態 + ``asyncio.Lock``、鎖內再檢
一次 TTL。錯誤處理：
    200                → 更新快取，回 name
    404 / 409          → 視為「沒有主簡報模型」，回 None（呼叫端退回 env 預設），只 log 一次
    連線失敗 / timeout  → 沿用上一次成功的值；從未成功過則回 None
    401 / 403          → 回 None + warning
"""
from __future__ import annotations

import logging
import time
from asyncio import Lock

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

SLIDES_PRIMARY_TTL_SECONDS = 60

_state: dict = {"model": None, "status": "unknown", "fetched_at": 0.0, "logged_not_found": False}
_lock = Lock()


def _headers() -> dict[str, str]:
    token = settings.CSP_SERVICE_TOKEN.strip()
    return {"X-CSP-Service-Token": token} if token else {}


def _log_not_found_once() -> None:
    if not _state["logged_not_found"]:
        logger.info(
            "slides-primary: csp 尚未設定主簡報模型（或已停用）— "
            "使用環境變數 ANILA_STUDIO_SLIDES_MODEL（此訊息只 log 一次）"
        )
        _state["logged_not_found"] = True


async def _refresh() -> None:
    async with _lock:
        now = time.time()
        if _state["status"] != "unknown" and now - _state["fetched_at"] < SLIDES_PRIMARY_TTL_SECONDS:
            return
        url = f"{settings.CSP_BASE_URL.rstrip('/')}/api/models/slides-primary"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url, headers=_headers())
        except Exception as exc:  # noqa: BLE001 — 拿不到旋鈕值只能退回 env，不能讓生成失敗
            logger.warning("slides-primary: 連線 csp 失敗（%s）— 沿用上次快取值", exc)
            _state["status"] = "conn_error"
            _state["fetched_at"] = now
            return
        if resp.status_code == 200:
            name = str((resp.json() or {}).get("name") or "").strip()
            if name:
                if _state["model"] != name:
                    logger.info("slides-primary: 主簡報模型 = %s", name)
                _state["model"] = name
                _state["status"] = "ok"
                _state["logged_not_found"] = False
            else:
                _state["model"] = None
                _state["status"] = "not_found"
                _log_not_found_once()
        elif resp.status_code in (404, 409):
            _state["model"] = None
            _state["status"] = "not_found"
            _log_not_found_once()
        elif resp.status_code in (401, 403):
            logger.warning("slides-primary: csp 拒絕 service token（%s）— 使用環境變數", resp.status_code)
            _state["model"] = None
            _state["status"] = "auth_error"
        else:
            logger.warning("slides-primary: csp 回應非預期 status=%s", resp.status_code)
            _state["status"] = "conn_error"
        _state["fetched_at"] = now


async def get_slides_primary() -> str | None:
    """csp 指定的主簡報模型名；沒有就 None（呼叫端退回 env 預設）。"""
    if time.time() - _state["fetched_at"] >= SLIDES_PRIMARY_TTL_SECONDS:
        await _refresh()
    if _state["status"] in ("ok", "conn_error"):
        return _state["model"]
    return None


async def resolve_model_name(requested: str, default: str) -> str:
    """呼叫端傳的是預設模型名 → 換成 csp 指定的主簡報模型（有的話）；明確指定別的模型不動。"""
    if requested != default:
        return requested
    return (await get_slides_primary()) or requested


def _reset_for_tests() -> None:
    _state.update({"model": None, "status": "unknown", "fetched_at": 0.0, "logged_not_found": False})


def _expire_for_tests() -> None:
    _state["fetched_at"] = 0.0
