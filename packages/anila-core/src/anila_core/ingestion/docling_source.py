"""文件解析位址的執行期來源。

CSP 與 ingestion-worker 各註冊自己的 provider（讀治理中心那一列）。
沒有註冊時，``ParserRegistry`` 仍走舊的 ``DOC_PARSER`` 環境變數，
給 anila-core 自己的單元測試用。平台行程一旦註冊，環境變數就不再決定
要不要打 Docling。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

# 30 秒：管理員改位址後，另一個行程最慢一個快取週期會跟上。
# 同一個行程在寫入時會呼叫 ``invalidate_docling_source_cache``。
DEFAULT_TTL_SECONDS = 30.0

_MISSING = object()


class DoclingSourceUnavailable(Exception):
    """控制面這次讀不到。呼叫端不得把它當成「沒設定 Docling」。"""


class DoclingMisconfigured(Exception):
    """已啟用但沒有可用位址。呼叫端不得退回原生解析器。"""


@dataclass(frozen=True)
class DoclingEndpoint:
    base_url: str
    token: str = ""


_lock = threading.Lock()
_provider: Callable[[], DoclingEndpoint | None] | None = None
_ttl = DEFAULT_TTL_SECONDS
_cached_at = 0.0
_cached_value: DoclingEndpoint | None | object = _MISSING


def docling_source_registered() -> bool:
    return _provider is not None


def register_docling_source(
    provider: Callable[[], DoclingEndpoint | None],
    *,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
) -> None:
    """註冊控制面。``provider`` 回 ``None`` 表示明確沒設定（用原生解析器）。

    擲出 ``DoclingMisconfigured`` 或 ``DoclingSourceUnavailable`` 時，
    這一層不會把結果改寫成 ``None``。
    """
    global _provider, _ttl
    with _lock:
        _provider = provider
        _ttl = ttl_seconds
        _cached_at = 0.0
        _cached_value = _MISSING


def reset_docling_source() -> None:
    """測試用：拆掉 provider 與快取，回到「沒註冊」."""
    global _provider, _ttl, _cached_at, _cached_value
    with _lock:
        _provider = None
        _ttl = DEFAULT_TTL_SECONDS
        _cached_at = 0.0
        _cached_value = _MISSING


def invalidate_docling_source_cache() -> None:
    global _cached_at, _cached_value
    with _lock:
        _cached_at = 0.0
        _cached_value = _MISSING


def resolve_docling_endpoint() -> DoclingEndpoint | None:
    """回傳目前該用的 Docling 位址。

    ``None``：控制面明確說沒設定。
    快取過期後重讀失敗、但手上還有上一筆成功結果（含「沒設定」）時，
    沿用上一筆，不在失敗的瞬間改走另一條路。
    從來沒讀成功過就擲出 ``DoclingSourceUnavailable``。
    """
    global _cached_at, _cached_value
    if _provider is None:
        raise DoclingSourceUnavailable("文件解析設定沒有註冊來源")

    now = time.monotonic()
    with _lock:
        fresh = _cached_value is not _MISSING and (now - _cached_at) < _ttl
        if fresh:
            return _cached_value  # type: ignore[return-value]
        provider = _provider
        previous = _cached_value

    try:
        value = provider()
    except DoclingMisconfigured:
        raise
    except DoclingSourceUnavailable:
        if previous is not _MISSING:
            return previous  # type: ignore[return-value]
        raise
    except Exception as exc:
        if previous is not _MISSING:
            return previous  # type: ignore[return-value]
        raise DoclingSourceUnavailable("文件解析設定讀取失敗") from exc

    if value is not None and not isinstance(value, DoclingEndpoint):
        raise DoclingSourceUnavailable("文件解析設定的型別不對")

    with _lock:
        _cached_value = value
        _cached_at = time.monotonic()
    return value
