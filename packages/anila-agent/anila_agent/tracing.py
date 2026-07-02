"""ANILA Full Trace 原生發送器（doc-05 §6 / doc-06 §6）。

這是**範本必備**：官方 agent starter 必須示範完整 span 集，真實 agent 照抄即可
接上 CSP 的 Full Trace（L3 approval blocker）。刻意**不依賴** ``anila_core`` ——
以最小的獨立 emitter 實作 FROZEN wire contract：

    POST {csp_base}/v1/traces/{trace_id}/spans
    JSON {"spans":[{span_id, parent_span_id?, span_type, name,
                    started_at, ended_at?, status, attributes?, producer:"agent"}...]}
    ≤256 spans/batch，Authorization: Bearer <Agent Integration Key(csk-)>，回 202。

設計不變量：
- **絕不讓 agent 掛掉**：ship 失敗一律 drop-and-log，flush 不拋例外。
- **無 trace_id / 無 endpoint → 完全停用**（zero behavior change；既有測試不受影響）。
- span_type 逐字採 doc-05 §6 的 13 種必備型別（started/finished 成對 + agent.error）。
- 巢狀關係以 ``contextvars`` 追蹤 current parent，併發下自動分艙（asyncio Task 各持 context 複本）。
"""

from __future__ import annotations

import contextlib
import logging
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from agents import RunHooks

logger = logging.getLogger("anila.trace")

# FROZEN：CSP ingestion 每批上限。
MAX_SPANS_PER_BATCH = 256

# doc-05 §6 span_type bases（started/finished 成對；ERROR 為單發）。
RUN = "agent.run"
STEP = "agent.step"
MODEL_CALL = "agent.model_call"
TOOL_CALL = "agent.tool_call"
RETRIEVAL = "agent.retrieval"
OUTPUT = "agent.output"
ERROR = "agent.error"

# 目前作用中的父 span id；巢狀 span 自動掛在其下。ContextVar 讓併發 run（各自 Task）
# 不互相污染父子關係。
_current_parent: ContextVar[str | None] = ContextVar("anila_trace_parent", default=None)

# 行內引用格式：【來源：doc_1】/【來源: a, b】。用於 final output span 的 citations 屬性。
_CITE_RE = re.compile(r"【來源[:：]\s*([^】]+)】")


def _now() -> str:
    """UTC ISO-8601（毫秒 + Z）。CSP 以此排序 span。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _new_id() -> str:
    return "span_" + uuid.uuid4().hex[:24]


def _preview(value: Any, limit: int = 200) -> str:
    text = value if isinstance(value, str) else repr(value)
    return text[:limit]


def extract_citations(text: str) -> list[str]:
    """從最終答案抽行內引用 id（去重、保序）。無引用回 []。"""
    if not text:
        return []
    ids: list[str] = []
    for group in _CITE_RE.findall(text):
        for part in re.split(r"[,，、;；\s]+", group.strip()):
            token = part.strip()
            if token and token not in ids:
                ids.append(token)
    return ids


class _Span:
    """一個進行中的邏輯 span：``.started`` 已送出，``finish()`` 送 ``.finished``。

    兩筆記錄共用同一 ``span_id`` 與 ``parent_span_id``（frozen shape 帶 started_at +
    ended_at?，故 started/finished 是同一 span 的兩個生命週期事件）。
    """

    __slots__ = ("_done", "_emitter", "attributes", "base", "name",
                 "parent_span_id", "span_id", "started_at")

    def __init__(self, emitter: TraceEmitter, base: str, name: str, span_id: str,
                 parent_span_id: str | None, started_at: str,
                 attributes: dict[str, Any] | None) -> None:
        self._emitter = emitter
        self.base = base
        self.name = name
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.started_at = started_at
        self.attributes: dict[str, Any] = dict(attributes or {})
        self._done = False

    def finish(self, *, status: str = "ok", attributes: dict[str, Any] | None = None) -> None:
        if self._done:
            return
        self._done = True
        if attributes:
            self.attributes.update(attributes)
        self._emitter._record(
            f"{self.base}.finished", self.name, self.span_id, self.parent_span_id,
            self.started_at, _now(), status, self.attributes,
        )


class TraceEmitter:
    """緩衝 + 批次 ship agent spans 回 CSP 的最小獨立發送器。

    ``active`` 為 False（缺 trace_id 或 endpoint，或 enable 旗標關）時所有方法皆 no-op，
    確保未帶 trace header 的請求零行為變化。
    """

    def __init__(
        self,
        *,
        trace_id: str | None,
        endpoint: str | None,
        api_key: str | None = None,
        agent_id: str | None = None,
        task_id: str | None = None,
        producer: str = "agent",
        enabled: bool = True,
        verify_ssl: bool = True,
        classification_level: str | None = None,
        batch_size: int = MAX_SPANS_PER_BATCH,
        timeout: float = 5.0,
    ) -> None:
        self.trace_id = (trace_id or "").strip() or None
        self.endpoint = ((endpoint or "").rstrip("/")) or None
        self.api_key = api_key or None
        self.agent_id = agent_id
        self.task_id = task_id
        self.producer = producer
        self.classification_level = classification_level
        self.verify_ssl = verify_ssl
        self.batch_size = max(1, min(int(batch_size), MAX_SPANS_PER_BATCH))
        self.timeout = timeout
        self._enabled = bool(enabled)
        self._buffer: list[dict[str, Any]] = []
        self.root_span_id: str | None = None

    @classmethod
    def from_context(
        cls,
        *,
        trace_id: str | None,
        task_id: str | None = None,
        endpoint: str | None = None,
        api_key: str | None = None,
        agent_id: str | None = None,
        enabled: bool = True,
        verify_ssl: bool = True,
        classification_level: str | None = None,
    ) -> TraceEmitter:
        """由 CSP dispatch header（X-ANILA-Trace-Id / X-ANILA-Task-Id）建 emitter。"""
        return cls(
            trace_id=trace_id, task_id=task_id, endpoint=endpoint, api_key=api_key,
            agent_id=agent_id, enabled=enabled, verify_ssl=verify_ssl,
            classification_level=classification_level,
        )

    @property
    def active(self) -> bool:
        return self._enabled and self.trace_id is not None and self.endpoint is not None

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
            "producer": self.producer,
        }
        if parent_span_id:
            span["parent_span_id"] = parent_span_id
        if ended_at:
            span["ended_at"] = ended_at
        if self.agent_id:
            span["agent_id"] = self.agent_id
        if attributes:
            span["attributes"] = dict(attributes)
        self._buffer.append(span)

    def begin(
        self, base: str, name: str, *, parent: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> _Span:
        """送出 ``{base}.started`` 並回傳可 ``finish()`` 的 span 控制代碼。

        ``parent`` 未給時用當前 contextvar（自動巢狀）；給了則強制掛在該 span 下。
        """
        span_id = _new_id()
        started_at = _now()
        parent_span_id = parent if parent is not None else _current_parent.get()
        self._record(f"{base}.started", name, span_id, parent_span_id,
                     started_at, None, "ok", attributes)
        return _Span(self, base, name, span_id, parent_span_id, started_at, attributes)

    def error(self, message: str, *, parent: str | None = None,
              attributes: dict[str, Any] | None = None) -> None:
        """送單發 ``agent.error`` span。"""
        attrs = dict(attributes or {})
        if message:
            attrs.setdefault("message", str(message)[:500])
        parent_span_id = parent if parent is not None else (_current_parent.get() or self.root_span_id)
        ts = _now()
        self._record(ERROR, "agent.error", _new_id(), parent_span_id, ts, ts, "error", attrs)

    # -- context managers -------------------------------------------------

    @asynccontextmanager
    async def span(self, base: str, name: str, *,
                   attributes: dict[str, Any] | None = None) -> AsyncIterator[_Span]:
        """一段自動巢狀的 span（started 進、finished 出；例外時補 error span）。"""
        if not self.active:
            yield _Span(self, base, name, "", None, "", attributes)
            return
        sp = self.begin(base, name, attributes=attributes)
        token = _current_parent.set(sp.span_id)
        try:
            yield sp
        except Exception as exc:
            _current_parent.reset(token)
            self.error(repr(exc), parent=sp.span_id)
            sp.finish(status="error")
            raise
        else:
            _current_parent.reset(token)
            sp.finish(status="ok")

    @asynccontextmanager
    async def run_span(self, name: str, *,
                       attributes: dict[str, Any] | None = None) -> AsyncIterator[_Span]:
        """root run span（parent=None）。分類等級隨 run + output span 帶出（doc-06 §6）。"""
        attrs = dict(attributes or {})
        if self.classification_level and "classification_level" not in attrs:
            attrs["classification_level"] = self.classification_level
        if self.task_id and "task_id" not in attrs:
            attrs["task_id"] = self.task_id
        if not self.active:
            yield _Span(self, RUN, name, "", None, "", attrs)
            return
        sp = self.begin(RUN, name, parent=None, attributes=attrs)
        self.root_span_id = sp.span_id
        token = _current_parent.set(sp.span_id)
        try:
            yield sp
        except Exception as exc:
            _current_parent.reset(token)
            self.error(repr(exc), parent=sp.span_id)
            sp.finish(status="error")
            self.root_span_id = None
            raise
        else:
            _current_parent.reset(token)
            sp.finish(status="ok")
            self.root_span_id = None

    # -- ship -------------------------------------------------------------

    async def flush(self) -> None:
        """把緩衝 span 分批 POST 回 CSP。失敗一律 drop-and-log，絕不拋例外。"""
        if not self.active or not self._buffer:
            return
        pending, self._buffer = self._buffer, []
        try:
            import httpx
        except Exception:  # pragma: no cover - httpx 是宣告相依，理論上必在
            logger.warning("httpx unavailable; dropping %d trace spans", len(pending))
            return
        url = f"{self.endpoint}/v1/traces/{self.trace_id}/spans"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            async with httpx.AsyncClient(verify=self.verify_ssl, timeout=self.timeout) as client:
                for i in range(0, len(pending), self.batch_size):
                    chunk = pending[i:i + self.batch_size]
                    resp = await client.post(url, headers=headers, json={"spans": chunk})
                    if resp.status_code >= 400:
                        logger.warning(
                            "trace ship rejected: HTTP %s (%d spans dropped)",
                            resp.status_code, len(chunk),
                        )
        except Exception as exc:
            # 網路/憑證/序列化任何失敗都不得傳播到 agent 執行路徑。
            logger.warning("trace ship failed: %s (%d spans dropped)", exc, len(pending))


def _model_name(agent: Any) -> str:
    model = getattr(agent, "model", None)
    return getattr(model, "model", None) or (str(model) if model else "model")


def _usage_attrs(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    return {
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
    }


class TracingRunHooks(RunHooks):
    """把 openai-agents 的 run 事件轉成 doc-05 span：step / model_call / tool_call。

    - ``on_agent_*`` → agent.step.*（掛在 run root 下）。
    - ``on_llm_*``   → agent.model_call.*（掛在當前 step 下）。
    - ``on_tool_*``  → agent.tool_call.*（掛在當前 step 下；並把 contextvar 設為此
      tool span，讓工具內部的 retrieval 自動巢狀在此 tool 下）。

    ``inner`` 可包既有 RunHooks（如 AuditHooks），事件會 fan-out 過去，稽核/計量不受影響。
    """

    def __init__(self, emitter: TraceEmitter, *, inner: RunHooks | None = None) -> None:
        self._e = emitter
        self._inner = inner
        self._step: _Span | None = None
        self._models: list[_Span] = []
        self._tools: dict[str, list[tuple[_Span, Any]]] = {}

    async def _fan(self, method: str, *args: Any) -> None:
        if self._inner is None:
            return
        fn = getattr(self._inner, method, None)
        if fn is not None:
            await fn(*args)

    async def on_agent_start(self, context: Any, agent: Any) -> None:
        name = getattr(agent, "name", "agent")
        self._step = self._e.begin(STEP, name, parent=self._e.root_span_id,
                                   attributes={"agent": name})
        await self._fan("on_agent_start", context, agent)

    async def on_agent_end(self, context: Any, agent: Any, output: Any) -> None:
        if self._step is not None:
            self._step.finish(status="ok")
            self._step = None
        await self._fan("on_agent_end", context, agent, output)

    async def on_llm_start(self, context: Any, agent: Any, system_prompt: Any,
                           input_items: Any) -> None:
        parent = self._step.span_id if self._step else self._e.root_span_id
        model = _model_name(agent)
        self._models.append(self._e.begin(MODEL_CALL, model, parent=parent,
                                          attributes={"model": model}))
        await self._fan("on_llm_start", context, agent, system_prompt, input_items)

    async def on_llm_end(self, context: Any, agent: Any, response: Any) -> None:
        if self._models:
            self._models.pop().finish(status="ok", attributes=_usage_attrs(response))
        await self._fan("on_llm_end", context, agent, response)

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        parent = self._step.span_id if self._step else self._e.root_span_id
        name = getattr(tool, "name", "tool")
        sp = self._e.begin(TOOL_CALL, name, parent=parent, attributes={"tool_name": name})
        # 讓工具內部的 retrieval span 自動掛在此 tool span 下。
        token = _current_parent.set(sp.span_id)
        self._tools.setdefault(name, []).append((sp, token))
        await self._fan("on_tool_start", context, agent, tool)

    async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
        name = getattr(tool, "name", "tool")
        stack = self._tools.get(name)
        if stack:
            sp, token = stack.pop()
            with contextlib.suppress(ValueError, LookupError):
                _current_parent.reset(token)
            sp.finish(status="ok", attributes={"output_preview": _preview(result)})
        await self._fan("on_tool_end", context, agent, tool, result)

    async def on_handoff(self, context: Any, from_agent: Any, to_agent: Any) -> None:
        await self._fan("on_handoff", context, from_agent, to_agent)


class TracingRetriever:
    """包裝任一 Retriever，於 ``search`` 前後送 agent.retrieval span（doc-05 §7）。

    符合 ``Retriever`` Protocol（name/metadata/search/fetch），可直接傳給 ``build_agent``。
    retrieval span 自動掛在當前 tool span 下（模型的 search_documents 工具內呼叫檢索）。
    """

    def __init__(self, inner: Any, emitter: TraceEmitter) -> None:
        self._inner = inner
        self._e = emitter

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def metadata(self) -> dict[str, Any]:
        return self._inner.metadata

    async def search(self, query: str, k: int = 5) -> list[Any]:
        meta: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            meta = self._inner.metadata or {}
        attrs: dict[str, Any] = {
            "top_k": k,
            "query_preview": _preview(query, 120),
            "source_scope": meta.get("backend", "collection"),
        }
        cid = meta.get("collection_id")
        if cid is not None:
            attrs["collection_ids"] = [cid]
        async with self._e.span(RETRIEVAL, self.name, attributes=attrs) as sp:
            hits = await self._inner.search(query, k=k)
            sp.attributes["chunk_ids"] = [getattr(h, "id", None) for h in hits]
            sp.attributes["document_ids"] = sorted({
                str(h.metadata.get("document_id"))
                for h in hits
                if isinstance(getattr(h, "metadata", None), dict)
                and h.metadata.get("document_id") is not None
            })
            sp.attributes["result_count"] = len(hits)
            return hits

    async def fetch(self, doc_id: str) -> Any:
        return await self._inner.fetch(doc_id)
