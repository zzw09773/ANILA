# -*- coding: utf-8 -*-
"""ANILA Full Trace 通用發送器（doc-05 §6 / doc-06 §6）——**可直接複製**版。

> 這支檔案的定位：讓「非 anila-agent」的第三方 runtime（LangChain、custom HTTP、
> 任何 OpenAI-compatible agent）用**最小相依（只需 httpx）**就能接上 ANILA CSP 的
> Full Trace（L3 approval blocker）。整包複製到你的 agent 專案即可，不必安裝
> ``anila-core`` 或 ``openai-agents``。

它實作了 **FROZEN wire contract**（不可更動；與 CSP ingestion 對齊）：

    POST {csp_base}/v1/traces/{trace_id}/spans
    Authorization: Bearer <Agent Integration Key（csk-）>
    Content-Type: application/json
    JSON {"spans":[{span_id, parent_span_id?, span_type, name,
                    started_at, ended_at?, status, attributes?, producer:"agent"}...]}
    每批 ≤256 spans，CSP 回 202。

設計不變量（照抄 packages/anila-agent/anila_agent/tracing.py 的 TraceEmitter 精神）：

- **絕不讓 agent 掛掉**：ship 失敗一律 drop-and-log，``flush()`` 不拋例外。
- **無 trace_id / 無 endpoint / 無 key → 完全停用**（``active`` 為 False，所有方法
  皆 no-op；未帶 trace header 的請求零行為變化、零外送）。
- ``span_type`` 逐字採 doc-05 §6 的 13 種必備型別（6 對 started/finished + agent.error）。
- 父子巢狀以每個 adapter 實例自持的堆疊追蹤（單一請求 = 單一 adapter 實例，
  各請求互不污染）；LangChain 這類「非巢狀成對回呼」則改用 ``begin()``＋
  ``Span.finish()`` 並以 ``parent=`` 明確指定父 span。

本檔刻意維持**同步**（``httpx.Client``）以求最大可攜性：LangChain 的
``BaseCallbackHandler`` 回呼是同步的，FastAPI 的同步 path operation 由 threadpool
執行也能安全阻塞。若你的 runtime 是 async，可在 executor / threadpool 內呼叫
``flush()``。
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("anila.trace.adapter")

# ── FROZEN 常數 ──────────────────────────────────────────────────────────────

# CSP ingestion 每批上限（FROZEN）。
MAX_SPANS_PER_BATCH = 256

# doc-05 §6 span_type 的 7 個 base（6 個成對 base + ERROR 單發）。
# ``span()`` / ``begin()`` 接收 base，進場送 ``{base}.started``、離場送
# ``{base}.finished``；``error()`` 送單發 ERROR。
RUN = "agent.run"
STEP = "agent.step"
MODEL_CALL = "agent.model_call"
TOOL_CALL = "agent.tool_call"
RETRIEVAL = "agent.retrieval"
OUTPUT = "agent.output"
ERROR = "agent.error"

#: 送 ``span()`` / ``begin()`` 的合法 base（不含 ERROR，ERROR 由 ``error()`` 專送）。
PAIRED_BASES: tuple[str, ...] = (RUN, STEP, MODEL_CALL, TOOL_CALL, RETRIEVAL, OUTPUT)

#: doc-05 §6「Required Span Types」的 13 種**逐字**必備型別（順序照文件）。
#: 測試以此對齊 doc-05 的清單，確保 adapter 與協定不漂移。
REQUIRED_SPAN_TYPES: tuple[str, ...] = (
    "agent.run.started",
    "agent.step.started",
    "agent.step.finished",
    "agent.model_call.started",
    "agent.model_call.finished",
    "agent.tool_call.started",
    "agent.tool_call.finished",
    "agent.retrieval.started",
    "agent.retrieval.finished",
    "agent.output.started",
    "agent.output.finished",
    "agent.error",
    "agent.run.finished",
)


def _now() -> str:
    """UTC ISO-8601（毫秒 + Z）。CSP 以此排序 span。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _new_id() -> str:
    return "span_" + uuid.uuid4().hex[:24]


class Span:
    """一個進行中的邏輯 span：建立時已送 ``{base}.started``，``finish()`` 送
    ``{base}.finished``（共用同一 ``span_id`` / ``parent_span_id``）。"""

    __slots__ = ("_adapter", "_done", "attributes", "base", "name",
                 "parent_span_id", "span_id", "started_at")

    def __init__(self, adapter: "AnilaTraceAdapter", base: str, name: str,
                 span_id: str, parent_span_id: str | None, started_at: str,
                 attributes: dict[str, Any] | None) -> None:
        self._adapter = adapter
        self.base = base
        self.name = name
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.started_at = started_at
        self.attributes: dict[str, Any] = dict(attributes or {})
        self._done = False

    def finish(self, *, status: str = "ok", **attrs: Any) -> None:
        if self._done:
            return
        self._done = True
        if attrs:
            self.attributes.update(attrs)
        self._adapter._record(
            f"{self.base}.finished", self.name, self.span_id, self.parent_span_id,
            self.started_at, _now(), status, self.attributes,
        )


class AnilaTraceAdapter:
    """緩衝 + 批次 ship agent spans 回 CSP 的最小、零額外相依發送器。

    :param csp_base: CSP 根 URL（如 ``https://anila.ai.ncsist.org.tw``）。
    :param integration_key: Agent Integration Key（``csk-`` 開頭；即 doc-08 的
        雙角色金鑰；由 CSP dispatch 的 ``Authorization: Bearer`` 帶入或註冊時取得）。
    :param trace_id: 由 CSP dispatch 的 ``X-ANILA-Trace-Id`` header 帶入；缺它則
        adapter 停用（no-op、零外送）。
    :param task_id: 選填，來自 ``X-ANILA-Task-Id``；隨 run span 帶出以利歸因。

    ``active`` 為 False（缺 trace_id、csp_base 或 integration_key，或 ``enabled=False``）
    時所有方法皆 no-op。
    """

    def __init__(
        self,
        csp_base: str | None,
        integration_key: str | None,
        trace_id: str | None,
        task_id: str | None = None,
        *,
        classification_level: str | None = None,
        enabled: bool = True,
        verify_ssl: bool = True,
        batch_size: int = MAX_SPANS_PER_BATCH,
        timeout: float = 5.0,
    ) -> None:
        self.csp_base = ((csp_base or "").rstrip("/")) or None
        self.integration_key = (integration_key or "").strip() or None
        self.trace_id = (trace_id or "").strip() or None
        self.task_id = (task_id or "").strip() or None
        self.classification_level = classification_level
        self.verify_ssl = verify_ssl
        self.batch_size = max(1, min(int(batch_size), MAX_SPANS_PER_BATCH))
        self.timeout = timeout
        self._enabled = bool(enabled)
        self._buffer: list[dict[str, Any]] = []
        self._parents: list[str] = []
        self.root_span_id: str | None = None

    # -- 狀態 -------------------------------------------------------------

    @property
    def active(self) -> bool:
        return bool(
            self._enabled
            and self.trace_id
            and self.csp_base
            and self.integration_key
        )

    def _current_parent(self) -> str | None:
        return self._parents[-1] if self._parents else None

    # -- 低階記錄 ---------------------------------------------------------

    def _record(
        self,
        span_type: str,
        name: str,
        span_id: str,
        parent_span_id: str | None,
        started_at: str,
        ended_at: str | None,
        status: str,
        attributes: dict[str, Any] | None,
    ) -> None:
        if not self.active:
            return
        span: dict[str, Any] = {
            "span_id": span_id,
            "span_type": span_type,
            "name": name,
            "started_at": started_at,
            "status": status,
            "producer": "agent",  # FROZEN：一律標記為 agent producer。
        }
        if parent_span_id:
            span["parent_span_id"] = parent_span_id
        if ended_at:
            span["ended_at"] = ended_at
        if attributes:
            span["attributes"] = dict(attributes)
        self._buffer.append(span)

    # -- span 生命週期 ----------------------------------------------------

    def begin(
        self, span_type: str, name: str, *,
        parent: str | None = None, **attrs: Any,
    ) -> Span:
        """送出 ``{span_type}.started`` 並回傳可 ``finish()`` 的 :class:`Span`。

        ``span_type`` 為 base（見 :data:`PAIRED_BASES`，如 :data:`TOOL_CALL`）。
        ``parent`` 未給時掛在目前堆疊頂端（自動巢狀）；給了則強制掛在該 span 下。
        供 LangChain 這類「非巢狀成對回呼」使用（begin 於 *_start、finish 於 *_end）。
        """
        span_id = _new_id()
        started_at = _now()
        parent_span_id = parent if parent is not None else self._current_parent()
        self._record(f"{span_type}.started", name, span_id, parent_span_id,
                     started_at, None, "ok", attrs or None)
        return Span(self, span_type, name, span_id, parent_span_id, started_at, attrs)

    @contextmanager
    def span(self, span_type: str, name: str, **attrs: Any) -> Iterator[Span]:
        """自動巢狀的成對 span：進場 ``{span_type}.started``、離場
        ``{span_type}.finished``；區塊內拋例外時補一筆 ``agent.error`` 並以
        ``status="error"`` 收尾後 re-raise。

        ``span_type`` 為 base；``agent.run`` 視為 root（parent=None）並登記
        ``root_span_id``、隨帶 task_id / classification_level。
        """
        is_run = span_type == RUN
        merged = dict(attrs)
        if is_run:
            if self.task_id and "task_id" not in merged:
                merged["task_id"] = self.task_id
            if self.classification_level and "classification_level" not in merged:
                merged["classification_level"] = self.classification_level
        if not self.active:
            yield Span(self, span_type, name, "", None, "", merged)
            return
        # run 為 root（強制 parent=None）；其餘掛在堆疊頂端（begin 預設行為）。
        sp = self.begin(span_type, name, parent=None, **merged) if is_run \
            else self.begin(span_type, name, **merged)
        if is_run:
            self.root_span_id = sp.span_id
        self._parents.append(sp.span_id)
        try:
            yield sp
        except Exception as exc:  # noqa: BLE001 - 補 error span 後照樣往外拋
            self._pop(sp.span_id)
            self.error(repr(exc), parent=sp.span_id)
            sp.finish(status="error")
            if is_run:
                self.root_span_id = None
            raise
        else:
            self._pop(sp.span_id)
            sp.finish(status="ok")
            if is_run:
                self.root_span_id = None

    def _pop(self, span_id: str) -> None:
        with contextlib.suppress(ValueError):
            self._parents.remove(span_id)

    def error(self, message: str, *, parent: str | None = None, **attrs: Any) -> None:
        """送單發 ``agent.error`` span（doc-05 §6 唯一的非成對型別）。"""
        merged = dict(attrs)
        if message:
            merged.setdefault("message", str(message)[:500])
        parent_span_id = parent if parent is not None else (
            self._current_parent() or self.root_span_id
        )
        ts = _now()
        self._record(ERROR, "agent.error", _new_id(), parent_span_id, ts, ts,
                     "error", merged or None)

    # -- ship -------------------------------------------------------------

    def flush(self) -> None:
        """把緩衝 span 分批 POST 回 CSP。失敗一律 drop-and-log，絕不拋例外。"""
        if not self.active or not self._buffer:
            return
        pending, self._buffer = self._buffer, []
        try:
            import httpx
        except Exception:  # pragma: no cover - httpx 是宣告相依，理論上必在
            logger.warning("httpx unavailable; dropping %d trace spans", len(pending))
            return
        url = f"{self.csp_base}/v1/traces/{self.trace_id}/spans"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.integration_key}",
        }
        try:
            with httpx.Client(verify=self.verify_ssl, timeout=self.timeout) as client:
                for i in range(0, len(pending), self.batch_size):
                    chunk = pending[i:i + self.batch_size]
                    resp = client.post(url, headers=headers, json={"spans": chunk})
                    if resp.status_code >= 400:
                        logger.warning(
                            "trace ship rejected: HTTP %s (%d spans dropped)",
                            resp.status_code, len(chunk),
                        )
        except Exception as exc:  # noqa: BLE001 - 任何網路/憑證/序列化失敗都不得傳播
            logger.warning("trace ship failed: %s (%d spans dropped)", exc, len(pending))
