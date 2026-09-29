"""anila-studio 打 CSP 用的服務憑證。

``ANILA_SERVICE_TOKEN_FILE`` 有設時只讀那個檔。檔案不在是
``file_missing``，讀不到或是空的是 ``file_error``。路徑沒設時來源是
``none``。檔案變更時重讀；呼叫端在 401／403
之後應再呼叫 :func:`reload` ``force=True`` 一次。

日誌與健康輸出只記來源名稱，不記明文。
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


def token() -> str:
    reload(False)
    return _token


def assert_no_legacy_shared_token() -> None:
    """共用權杖已退役。只看環境變數，抓手動 ``docker run -e``。"""
    present: list[str] = []
    for name in ("CSP_SERVICE_TOKEN", "CSP_BOOTSTRAP_TOKEN"):
        if os.environ.get(name, "").strip():
            present.append(name)
    if not present:
        return
    names = "、".join(present)
    raise RuntimeError(
        f"拒絕啟動：請從 .env 刪除 {names}。"
        "各服務已改讀 ANILA_SERVICE_TOKEN_FILE 的專屬憑證，不再使用共用權杖。"
    )


def headers() -> dict[str, str]:
    value = token()
    if not value:
        return {}
    return {"X-CSP-Service-Token": value}


def health() -> dict[str, str]:
    return {"token_source": source()}


def reload(force: bool = False) -> None:
    """重讀憑證。``force`` 用於 401／403 之後那一次，不看 mtime。"""
    global _token, _source, _mtime_ns
    path = _path()
    if path is None:
        _publish("", "none")
        _mtime_ns = None
        return
    # 這個檔案系統覆寫後 mtime 可能不變，所以每次都讀內容。檔案只有一行。
    del force
    value, found = _read(path)
    if value == _token and found == _source:
        _remember_mtime(path)
        return
    _publish(value, found)
    logger.info("studio service token reloaded from %s", found)


def _read(path: Path) -> tuple[str, str]:
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        logger.error("studio token file is missing; not using fallback credentials")
        return "", "file_missing"
    except OSError as exc:
        logger.error(
            "studio token file cannot be stat'ed (%s); not using fallback credentials",
            exc.__class__.__name__,
        )
        return "", "file_error"
    if not stat.S_ISREG(mode):
        logger.error("studio token file is not a regular file; not using fallback credentials")
        return "", "file_error"
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.error(
            "studio token file is unreadable (%s); not using fallback credentials",
            exc.__class__.__name__,
        )
        return "", "file_error"
    if not value:
        logger.error("studio token file is empty; not using fallback credentials")
        return "", "file_error"
    return value, "file"


def _publish(value: str, found: str) -> None:
    global _token, _source
    _token = value
    _source = found
    path = _path()
    _remember_mtime(path)


def _remember_mtime(path: Path | None) -> None:
    global _mtime_ns
    if path is None or not path.is_file():
        _mtime_ns = None
        return
    try:
        _mtime_ns = path.stat().st_mtime_ns
    except OSError:
        _mtime_ns = None
