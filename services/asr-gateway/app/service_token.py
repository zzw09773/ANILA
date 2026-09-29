"""asr-gateway 打 CSP 用的服務憑證。

``ANILA_SERVICE_TOKEN_FILE`` 有設時只讀那個檔。檔案不在就是
``file_missing``，不改用 ``CSP_SERVICE_TOKEN``。路徑沒設時來源是 ``none``。
日誌只記來源，不記明文。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_token = ""
_source = "none"


def assert_no_legacy_shared_token() -> None:
    """共用權杖已退役。環境裡還留著非空值就拒絕啟動。"""
    present = [
        name
        for name in ("CSP_SERVICE_TOKEN", "CSP_BOOTSTRAP_TOKEN")
        if os.environ.get(name, "").strip()
    ]
    if not present:
        return
    names = "、".join(present)
    raise RuntimeError(
        f"拒絕啟動：請從 .env 刪除 {names}。"
        "各服務已改讀 ANILA_SERVICE_TOKEN_FILE 的專屬憑證，不再使用共用權杖。"
    )


def token() -> str:
    reload()
    return _token


def source() -> str:
    reload()
    return _source


def reload() -> None:
    global _token, _source
    raw = os.environ.get("ANILA_SERVICE_TOKEN_FILE", "").strip()
    if not raw:
        _token = ""
        _source = "none"
        return
    path = Path(raw)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        logger.error("asr-gateway token file is missing")
        _token = ""
        _source = "file_missing"
        return
    except OSError as exc:
        logger.error("asr-gateway token file cannot be read (%s)", type(exc).__name__)
        _token = ""
        _source = "file_error"
        return
    if not value:
        _token = ""
        _source = "file_error"
        return
    _token = value
    _source = "file"