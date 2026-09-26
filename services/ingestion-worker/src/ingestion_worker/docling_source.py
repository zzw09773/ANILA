"""向 CSP 問文件解析位址。

用憑證檔裡的 sk-。worker 不讀那張設定表，也解不開語音憑證。
短快取在 anila-core 那一層。連線池參數留著，是因為既有工作還會傳進來。
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from anila_core.ingestion.docling_source import (
    DoclingEndpoint,
    DoclingMisconfigured,
    DoclingSourceUnavailable,
    invalidate_docling_source_cache,
    register_docling_source,
)
from ingestion_worker.credential_file import api_key as credential_api_key

logger = logging.getLogger(__name__)

_state: dict[str, Any] = {
    "ready": False,
    "error": False,
    "endpoint": None,
    "misconfigured": False,
}


def install() -> None:
    register_docling_source(_provider)


def _provider() -> DoclingEndpoint | None:
    if _state["error"] and not _state["ready"]:
        raise DoclingSourceUnavailable("ingestion-worker 讀不到文件解析設定")
    if not _state["ready"]:
        raise DoclingSourceUnavailable("文件解析設定還沒讀過")
    if _state["misconfigured"]:
        raise DoclingMisconfigured("文件解析已啟用但沒有位址")
    return _state["endpoint"]


def _csp_base() -> str:
    raw = os.environ.get("CSP_BASE_URL", "").strip()
    if raw:
        return raw.rstrip("/")
    return "http://csp:8000"


def _apply_payload(body: dict) -> None:
    _state["ready"] = True
    _state["error"] = False
    _state["misconfigured"] = False
    _state["endpoint"] = None
    if not body.get("enabled"):
        return
    url = (body.get("base_url") or "").strip()
    if not url or body.get("misconfigured"):
        _state["misconfigured"] = True
        return
    token = (body.get("credential") or "").strip()
    _state["endpoint"] = DoclingEndpoint(base_url=url, token=token)


async def refresh_document_parser(pool: Any = None) -> None:
    """每個工作開始前問一次。問失敗時留著上一筆，沒有上一筆就讓解析失敗。"""
    del pool
    install()
    token = credential_api_key("").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = f"{_csp_base()}/api/internal/external-services/document_parser"
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
            response = await client.get(url, headers=headers)
        if response.status_code != 200:
            raise RuntimeError(f"status {response.status_code}")
        body = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("payload")
    except Exception:
        logger.warning("ingestion-worker: 讀取文件解析設定失敗")
        if not _state["ready"]:
            _state["error"] = True
        invalidate_docling_source_cache()
        return
    _apply_payload(body)
    invalidate_docling_source_cache()


def reset_for_tests() -> None:
    _state["ready"] = False
    _state["error"] = False
    _state["endpoint"] = None
    _state["misconfigured"] = False
