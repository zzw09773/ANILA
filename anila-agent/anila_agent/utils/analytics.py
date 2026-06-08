"""P2-15 / claude-code §4.23 — log_event 結構化分析事件。

claude-code-src ``src/services/analytics/*`` 用 ``logEvent('tengu_xxx', metadata)``
形式發 analytics event。anila 對應的設計目標:

- **結構化**:event 走 dataclass / schema,**不再** ``print()`` / ``logger.info()``
  散裝;name + props + timestamp 一律走 :class:`AnalyticsEvent`。
- **可選分流**:emit 時可選一條或多條 sink,常見:
  * ``LoggingSink``  → 走 stdlib ``logging`` 結構化輸出。
  * ``InMemorySink`` → 攔截下來給 test / debug;預設 1000 容量 ring buffer。
  * ``CallableSink`` → user-supplied callback(送到 ANILA platform analytics
    HTTP endpoint 走這個)。
- **不引新 dep**:純 stdlib;真要送 HTTP / Kafka 由呼叫端寫 ``CallableSink``。
- **命名建議**:遵循 ``anila_<area>_<verb>`` 格式(對齊 claude-code ``tengu_*``)。
  常數 prefix 由 :data:`EVENT_PREFIX` 提供;helper :func:`canonical_event_name`
  可自動補。

不接 ``tracing`` 模組 —— analytics event 跟 trace span 的職責不同:span 在描述
operation 邊界與依賴(用於 debug),event 在描述「business / product 指標」
(用於 funnel / churn / 成本分析)。兩者可同時 emit。
"""

from __future__ import annotations

import logging
import os
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

# event 名稱 prefix(對齊 claude-code 的 ``tengu_*``)。
EVENT_PREFIX: str = "anila_"

# in-memory sink 預設容量 —— 比 error_buffer 大一個量級,因為 event 量本來就多。
DEFAULT_INMEMORY_CAPACITY: int = 1000

# 可選環境變數:全域關閉 analytics(壓力測試 / 隱私情境)。
_ENV_DISABLED = "ANILA_ANALYTICS_DISABLED"

_logger = logging.getLogger(__name__)


def canonical_event_name(name: str) -> str:
    """確保 event name 有 :data:`EVENT_PREFIX` prefix。

    呼叫端可傳 ``"agent_start"`` 也可傳 ``"anila_agent_start"`` —— 統一 normalize
    成後者。空字串視為 invalid。
    """
    if not name:
        raise ValueError("event name must be non-empty")
    if name.startswith(EVENT_PREFIX):
        return name
    return f"{EVENT_PREFIX}{name}"


@dataclass(frozen=True)
class AnalyticsEvent:
    """單筆 analytics event(immutable 值物件)。

    Attributes:
        name: canonical name(``anila_*``)。
        properties: 任意 JSON-serializable metadata(model、latency_ms、
            cost_usd、success 之類)。
        timestamp: emit 時間(UTC)。
        chain_id: 對應 :class:`tracing.Trace.chain_id`,跨多 agent 聚合用;
            ``None`` 表示未掛 chain。
    """

    name: str
    properties: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    chain_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化為 dict(供 sink 輸出 / HTTP 送出)。"""
        return {
            "name": self.name,
            "properties": dict(self.properties),
            "timestamp": self.timestamp.isoformat(),
            "chain_id": self.chain_id,
        }


# ---------------------------------------------------------------------------
# Sink 介面 + 內建實作
# ---------------------------------------------------------------------------


class AnalyticsSink(Protocol):
    """analytics 接收端 protocol。

    實作者只要提供 ``emit(event)`` 即可;raise 不會中斷 :class:`AnalyticsEmitter`
    其他 sink(emit 端會 swallow exception 並 log warn)。
    """

    def emit(self, event: AnalyticsEvent) -> None: ...


class LoggingSink:
    """把 event 走 stdlib ``logging`` 輸出。

    格式為單行 ``INFO event=<name> chain_id=<id> props=<dict-repr>``;運維端可
    透過 log aggregator(filebeat / fluentbit)直接 ingest。
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or _logger

    def emit(self, event: AnalyticsEvent) -> None:
        self._logger.info(
            "event=%s chain_id=%s props=%r",
            event.name,
            event.chain_id or "-",
            event.properties,
        )


class InMemorySink:
    """ring buffer sink —— 給 test / debug / `/analytics-dump` 用。"""

    def __init__(self, capacity: int = DEFAULT_INMEMORY_CAPACITY) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._buf: deque[AnalyticsEvent] = deque(maxlen=capacity)

    def emit(self, event: AnalyticsEvent) -> None:
        self._buf.append(event)

    def snapshot(self) -> list[AnalyticsEvent]:
        """回 buffer 副本(舊到新)。"""
        return list(self._buf)

    def latest(self, n: int = 10) -> list[AnalyticsEvent]:
        if n <= 0:
            return []
        return list(self._buf)[-n:]

    def clear(self) -> None:
        self._buf.clear()


class CallableSink:
    """把 emit 委派給任意 callable;用於 HTTP / Kafka / 自家 platform。

    callable signature: ``(event: AnalyticsEvent) -> None``。raise 會被
    :class:`AnalyticsEmitter` 攔下,不影響其他 sink。
    """

    def __init__(self, callback: Callable[[AnalyticsEvent], None]) -> None:
        self._callback = callback

    def emit(self, event: AnalyticsEvent) -> None:
        self._callback(event)


# ---------------------------------------------------------------------------
# Emitter
# ---------------------------------------------------------------------------


class AnalyticsEmitter:
    """串接一條或多條 sink,提供唯一 ``emit()`` 入口。

    若 :data:`_ENV_DISABLED` 環境變數設成 truthy(``1`` / ``true`` / ``yes``),
    ``emit`` 變 no-op。便於壓力測試 / CI 關掉 analytics。
    """

    def __init__(self, sinks: list[AnalyticsSink] | None = None) -> None:
        self._sinks: list[AnalyticsSink] = list(sinks) if sinks else []

    @property
    def sinks(self) -> list[AnalyticsSink]:
        return list(self._sinks)

    def add_sink(self, sink: AnalyticsSink) -> None:
        self._sinks.append(sink)

    def emit(
        self,
        name: str,
        properties: dict[str, Any] | None = None,
        *,
        chain_id: str | None = None,
    ) -> AnalyticsEvent | None:
        """組 event + 對所有 sink 廣播。

        - global disabled → return None,不 emit。
        - 沒有 sink → 一樣組 event 但無人接收,return event 物件給呼叫端
          自行處理(便於組裝 event 但延後送出的 pattern)。
        - sink raise → log warn 並繼續其他 sink。
        """
        if _is_disabled():
            return None

        event = AnalyticsEvent(
            name=canonical_event_name(name),
            properties=dict(properties) if properties else {},
            chain_id=chain_id,
        )
        for sink in self._sinks:
            try:
                sink.emit(event)
            except Exception as exc:  # noqa: BLE001 — sink 例外不該擴散
                _logger.warning(
                    "analytics sink %s raised: %s",
                    type(sink).__name__,
                    exc,
                )
        return event


def _is_disabled() -> bool:
    raw = os.environ.get(_ENV_DISABLED, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Module-level singleton + 便利函式
# ---------------------------------------------------------------------------


_global_emitter: AnalyticsEmitter | None = None


def get_analytics_emitter() -> AnalyticsEmitter:
    """取得 process-wide singleton emitter。

    第一次呼叫不掛任何 sink — 呼叫端要顯式 add_sink。這跟 ``error_buffer`` 不同
    (error_buffer 預設立即可用),理由:analytics 預期 sink 是配置決定的
    (走自家 endpoint vs. 走 log),不該隱式預設。
    """
    global _global_emitter
    if _global_emitter is None:
        _global_emitter = AnalyticsEmitter()
    return _global_emitter


def reset_analytics_emitter(sinks: list[AnalyticsSink] | None = None) -> AnalyticsEmitter:
    """重設 singleton(test fixture 用)。"""
    global _global_emitter
    _global_emitter = AnalyticsEmitter(sinks=sinks)
    return _global_emitter


def log_event(
    name: str,
    properties: dict[str, Any] | None = None,
    *,
    chain_id: str | None = None,
) -> AnalyticsEvent | None:
    """便利函式:對 global singleton emit 一筆 event。

    對齊 claude-code ``logEvent('tengu_*', meta)`` 的呼叫慣例。
    """
    return get_analytics_emitter().emit(name, properties, chain_id=chain_id)


__all__ = [
    "DEFAULT_INMEMORY_CAPACITY",
    "EVENT_PREFIX",
    "AnalyticsEmitter",
    "AnalyticsEvent",
    "AnalyticsSink",
    "CallableSink",
    "InMemorySink",
    "LoggingSink",
    "canonical_event_name",
    "get_analytics_emitter",
    "log_event",
    "reset_analytics_emitter",
]
