"""P2-15 / claude-code §4.23 — getInMemoryErrors ring buffer。

claude-code-src ``src/utils/log.js`` 把最近 N 個 error 保留在 process memory
裡(``getInMemoryErrors()``),提供 ``/doctor``、final result note 與 ServerSent
event metadata 取用。對應 anila 場景:

- ``AnilaRunner`` 跑完一輪後,在 ``RunSummary.metadata`` 帶最近 errors;
- hook handler / tool wrapper 出現非致命 exception 時,push 到 buffer;
- HTTP /SSE diagnostic endpoint 直接 dump buffer 給維運用。

本模組刻意不接 ``logging`` handler —— 我們**不**想把每筆 log 都往這個 buffer
推(成本太高 & 噪音)。push 是顯式呼叫,呼叫端決定哪些值得記。

設計重點
--------
- ``collections.deque(maxlen=N)`` 為底層,O(1) push、自動淘汰最舊。
- ``ErrorRecord`` 是 ``frozen dataclass``,值物件;serialize 給 JSONL / SSE。
- 預設 buffer size 100(與 claude-code 一致),可由 :func:`get_error_buffer`
  注入或環境變數 ``ANILA_ERROR_BUFFER_SIZE`` 改。
- 提供 **module-level global singleton** + **顯式 instance** 兩種介面 ——
  global 給「跨 component 統一視圖」用,instance 給 test isolation 用。
- thread-safety:`deque.append` 是 atomic GIL operation,讀寫不需要鎖。為
  跨 thread snapshot 一致性,``snapshot()`` 用 ``list(self._buf)`` (deque
  iter 在改寫時 raise,但 snapshot 拷貝一次)。

不引新 dep,純 stdlib。
"""

from __future__ import annotations

import os
import traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

DEFAULT_BUFFER_SIZE: int = 100

# 環境變數覆寫(部署可以調小,例如壓力測試時避免過多 retention)。
_ENV_BUFFER_SIZE = "ANILA_ERROR_BUFFER_SIZE"


@dataclass(frozen=True)
class ErrorRecord:
    """單筆 error 的不可變快照。

    Attributes:
        timestamp: 發生時刻(UTC)。
        source: 觸發來源(例如 ``"runner"`` / ``"tool.bash"`` / ``"hook.command"``);
            供 filter / group-by 用。
        kind: error class name(``type(exc).__name__``)。
        message: ``str(exc)``。
        traceback_text: 完整 traceback 字串(``traceback.format_exception``);
            若 push 時沒有 exception,為 ``None``。
        context: 額外 metadata(任意 JSON-serializable 內容)。
    """

    timestamp: datetime
    source: str
    kind: str
    message: str
    traceback_text: str | None = None
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化為 dict(供 JSONL / SSE)。"""
        return {
            "timestamp": self.timestamp.isoformat(),
            "source": self.source,
            "kind": self.kind,
            "message": self.message,
            "traceback": self.traceback_text,
            "context": dict(self.context),
        }


class InMemoryErrorBuffer:
    """固定容量 ring buffer,留最近 N 個 ``ErrorRecord``。

    Args:
        max_size: 最多保留多少筆;到上限後新筆會擠掉最舊。<=0 視為 invalid。
    """

    def __init__(self, max_size: int = DEFAULT_BUFFER_SIZE) -> None:
        if max_size <= 0:
            raise ValueError("max_size must be positive")
        self._buf: deque[ErrorRecord] = deque(maxlen=max_size)
        self._max_size = max_size

    @property
    def max_size(self) -> int:
        return self._max_size

    def __len__(self) -> int:
        return len(self._buf)

    def push(
        self,
        *,
        source: str,
        error: BaseException | None = None,
        message: str | None = None,
        kind: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> ErrorRecord:
        """加入一筆 error。

        - 若給了 ``error``,自動抽 ``kind`` / ``message`` / ``traceback_text``;
          ``message`` 顯式傳會覆寫。
        - 沒給 ``error`` 時 ``message`` 必填(代表純 logical error 不是 exception)。
        - 回傳剛 push 的 ``ErrorRecord`` 供呼叫端 chain (例如同步寫 trace)。
        """
        if error is None and not message:
            raise ValueError("must provide either error or message")

        record = ErrorRecord(
            timestamp=datetime.now(timezone.utc),
            source=source,
            kind=kind or (type(error).__name__ if error is not None else "Error"),
            message=message or (str(error) if error is not None else ""),
            traceback_text=(
                "".join(
                    traceback.format_exception(type(error), error, error.__traceback__)
                )
                if error is not None
                else None
            ),
            context=dict(context) if context else {},
        )
        self._buf.append(record)
        return record

    def snapshot(self) -> list[ErrorRecord]:
        """回傳當下 buffer 的副本(從舊到新)。"""
        return list(self._buf)

    def latest(self, n: int = 10) -> list[ErrorRecord]:
        """回最近 n 筆(從舊到新)。``n`` 超過 buffer 長度時回全部。"""
        if n <= 0:
            return []
        if n >= len(self._buf):
            return list(self._buf)
        # deque slice 不支援,用 list 再切。
        return list(self._buf)[-n:]

    def filter_by_source(self, source: str) -> list[ErrorRecord]:
        """回所有 ``source`` 等於指定值的 record(從舊到新)。"""
        return [r for r in self._buf if r.source == source]

    def clear(self) -> None:
        """清空 buffer(主要給 test 用)。"""
        self._buf.clear()


# ---------------------------------------------------------------------------
# Module-level singleton(可由環境變數調 size)
# ---------------------------------------------------------------------------


def _resolve_default_size() -> int:
    """依環境變數決定預設 buffer size;invalid → fallback default。"""
    raw = os.environ.get(_ENV_BUFFER_SIZE)
    if not raw:
        return DEFAULT_BUFFER_SIZE
    try:
        n = int(raw)
        if n > 0:
            return n
    except ValueError:
        pass
    return DEFAULT_BUFFER_SIZE


_global_buffer: InMemoryErrorBuffer | None = None


def get_error_buffer() -> InMemoryErrorBuffer:
    """取得 process-wide singleton ring buffer。

    第一次呼叫時依環境變數決定 size。test 想要 isolation 請 ``reset_error_buffer()``
    或自己 instantiate :class:`InMemoryErrorBuffer`。
    """
    global _global_buffer
    if _global_buffer is None:
        _global_buffer = InMemoryErrorBuffer(max_size=_resolve_default_size())
    return _global_buffer


def reset_error_buffer(max_size: int | None = None) -> InMemoryErrorBuffer:
    """重設 singleton(主要給 test fixture 用)。回傳新 buffer。"""
    global _global_buffer
    size = max_size if max_size is not None else _resolve_default_size()
    _global_buffer = InMemoryErrorBuffer(max_size=size)
    return _global_buffer


def record_error(
    *,
    source: str,
    error: BaseException | None = None,
    message: str | None = None,
    kind: str | None = None,
    context: dict[str, Any] | None = None,
) -> ErrorRecord:
    """便利函式:往 global singleton push 一筆 error。"""
    return get_error_buffer().push(
        source=source,
        error=error,
        message=message,
        kind=kind,
        context=context,
    )


__all__ = [
    "DEFAULT_BUFFER_SIZE",
    "ErrorRecord",
    "InMemoryErrorBuffer",
    "get_error_buffer",
    "record_error",
    "reset_error_buffer",
]
