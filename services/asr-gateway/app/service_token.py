"""asr-gateway 打 CSP 用的服務憑證。

``ANILA_SERVICE_TOKEN_FILE`` 有設時只讀那個檔。檔案不在就不改用
``CSP_SERVICE_TOKEN``。路徑沒設時，呼叫端改讀自己手上的 Settings。
日誌只記來源，不記明文。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_token = ""
_source = "none"


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