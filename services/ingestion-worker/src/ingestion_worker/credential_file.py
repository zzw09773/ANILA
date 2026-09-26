"""ingestion-worker 打 CSP 用的 sk-。

``ANILA_SERVICE_TOKEN_FILE`` 有設時，嵌入、視覺、關係抽取都用這一個檔。
檔案不在是 ``file_missing``，讀不到或是空的是 ``file_error``。兩種都不
改用環境變數裡的 API key。檔案變更（mtime）時重讀；401／403 之後呼叫
:func:`reload` ``force=True`` 一次。

路徑沒設時，``api_key`` 原樣交回呼叫端傳入的那把金鑰（既有測試與
未設檔案的部署）。健康輸出只記來源，不記明文。
"""
from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

_token = ""
_source = "none"
_mtime_ns: int | None = None


def _path() -> Path | None:
    raw = os.environ.get("ANILA_SERVICE_TOKEN_FILE", "").strip()
    return Path(raw) if raw else None


def source() -> str:
    reload(False)
    return _source


def credential_health() -> dict[str, str]:
    return {"token_source": source()}


def api_key(fallback: str) -> str:
    """路徑有設時只回檔案內容（可能是空字串）。否則回 ``fallback``。"""
    reload(False)
    if _path() is not None:
        return _token
    return fallback


def reload(force: bool = False) -> None:
    global _token, _source, _mtime_ns
    path = _path()
    if path is None:
        _publish("", "none")
        _mtime_ns = None
        return
    # 覆寫後 mtime 可能不變，所以每次都讀內容。檔案只有一行。
    del force
    value, found = _read(path)
    if value == _token and found == _source:
        _remember_mtime(path)
        return
    _publish(value, found)
    logger.info("ingestion-worker credential reloaded from %s", found)


def _read(path: Path) -> tuple[str, str]:
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        logger.error("worker credential file is missing; not using fallback credentials")
        return "", "file_missing"
    except OSError as exc:
        logger.error(
            "worker credential file cannot be stat'ed (%s); not using fallback credentials",
            exc.__class__.__name__,
        )
        return "", "file_error"
    if not stat.S_ISREG(mode):
        logger.error(
            "worker credential file is not a regular file; not using fallback credentials"
        )
        return "", "file_error"
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.error(
            "worker credential file is unreadable (%s); not using fallback credentials",
            exc.__class__.__name__,
        )
        return "", "file_error"
    if not value:
        logger.error("worker credential file is empty; not using fallback credentials")
        return "", "file_error"
    return value, "file"


def _publish(value: str, found: str) -> None:
    global _token, _source
    _token = value
    _source = found
    _remember_mtime(_path())


def _remember_mtime(path: Path | None) -> None:
    global _mtime_ns
    if path is None or not path.is_file():
        _mtime_ns = None
        return
    try:
        _mtime_ns = path.stat().st_mtime_ns
    except OSError:
        _mtime_ns = None
