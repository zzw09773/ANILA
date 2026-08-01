# -*- coding: utf-8 -*-
"""LangChain → ANILA Full Trace adapter（doc-05 §9「LangChain」）。

把 LangChain 的 callback 事件映射成 doc-05 §6 的 span 集，餵給
:class:`AnilaTraceAdapter`：

    on_chain_start / on_chain_end   → agent.run（最外層）/ agent.step（巢狀）
    on_llm_start   / on_llm_end     → agent.model_call
    on_tool_start  / on_tool_end    → agent.tool_call
    on_retriever_* / *_end          → agent.retrieval
    on_*_error                      → agent.error（並以 status="error" 收尾）

**LangChain 未安裝也能 import**：本檔以 try/except 保護 ``BaseCallbackHandler``
匯入；handler 類別只有在 LangChain 可用時才定義（見 ``_LANGCHAIN_AVAILABLE``）。
如此本檔可被 lint / import / 單元測試，而不必把 LangChain 拉進 air-gap 環境。

真實接法：把 ``AnilaLangChainTracer`` 加進 LangChain 執行時的 ``callbacks=[...]``，
trace_id / task_id / 派工 JWT 由 CSP dispatch 的 header 帶入
（``X-ANILA-Trace-Id`` / ``X-ANILA-Task-Id`` / ``Authorization: Bearer <JWT>``）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

# 讓「複製整個 examples/trace-adapters/」的人不必先 pip install：把 common/ 上到 path。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))

from anila_trace_adapter import (  # noqa: E402  (path 注入後才 import)
    MODEL_CALL,
    RETRIEVAL,
    RUN,
    STEP,
    TOOL_CALL,
    AnilaTraceAdapter,
    Span,
)

# ── LangChain 匯入守衛（未安裝時不炸；handler 類別僅在可用時定義）──────────────
try:  # 新版 LangChain
    from langchain_core.callbacks.base import BaseCallbackHandler

    _LANGCHAIN_AVAILABLE = True
except ImportError:  # pragma: no cover - CI 無 langchain
    try:  # 舊版 fallback
        from langchain.callbacks.base import BaseCallbackHandler  # type: ignore

        _LANGCHAIN_AVAILABLE = True
    except ImportError:
        _LANGCHAIN_AVAILABLE = False


_NOT_AVAILABLE_MSG = (
    "尚未安裝 LangChain（langchain-core）——本 adapter 需要它才能運作。\n"
    "請在你的 agent 環境執行：pip install langchain-core\n"
    "（common/anila_trace_adapter.py 本身只需 httpx，不受此限制。）"
)


def adapter_from_env(**overrides: Any) -> AnilaTraceAdapter:
    """從 CSP dispatch 常見環境變數建 :class:`AnilaTraceAdapter`。

    缺 ``ANILA_TRACE_ID`` 時 adapter 自動停用（no-op、零外送）。
    """
    return AnilaTraceAdapter(
        csp_base=overrides.get("csp_base", os.environ.get("ANILA_CSP_BASE")),
        dispatch_token=overrides.get(
            "dispatch_token",
            overrides.get(
                "integration_key",
                os.environ.get("ANILA_DISPATCH_TOKEN")
                or os.environ.get("ANILA_INTEGRATION_KEY"),
            ),
        ),
        trace_id=overrides.get("trace_id", os.environ.get("ANILA_TRACE_ID")),
        task_id=overrides.get("task_id", os.environ.get("ANILA_TASK_ID")),
        classification_level=overrides.get(
            "classification_level", os.environ.get("ANILA_CLASSIFICATION_LEVEL")
        ),
    )


if _LANGCHAIN_AVAILABLE:

    class AnilaLangChainTracer(BaseCallbackHandler):
        """LangChain callback handler，將 run tree 映射為 ANILA span。

        以 ``run_id`` → :class:`Span` 的字典追蹤在途 span，並用 ``parent_run_id``
        還原父子關係（LangChain 已把巢狀關係編碼在這兩個 UUID）。最外層
        chain（``parent_run_id is None``）對應 ``agent.run`` root，結束時自動
        ``flush()`` 把 span 批次送回 CSP。
        """

        # LangChain 需要這面旗標才會把 prompt / input 傳進回呼。
        raise_error = False

        def __init__(self, adapter: AnilaTraceAdapter) -> None:
            super().__init__()
            self._adapter = adapter
            self._spans: dict[UUID, Span] = {}

        # -- 內部工具 -----------------------------------------------------

        def _parent_span_id(self, parent_run_id: UUID | None) -> str | None:
            if parent_run_id is None:
                return None
            sp = self._spans.get(parent_run_id)
            return sp.span_id if sp else self._adapter.root_span_id

        def _open(self, run_id: UUID, parent_run_id: UUID | None,
                  base: str, name: str, **attrs: Any) -> None:
            parent = self._parent_span_id(parent_run_id)
            if base == RUN:
                # root run：登記 root_span_id 供錯誤 / 子 span 掛靠。
                sp = self._adapter.begin(RUN, name, parent=None, **attrs)
                self._adapter.root_span_id = sp.span_id
            else:
                sp = self._adapter.begin(base, name, parent=parent, **attrs)
            self._spans[run_id] = sp

        def _close(self, run_id: UUID, *, status: str = "ok", **attrs: Any) -> None:
            sp = self._spans.pop(run_id, None)
            if sp is not None:
                sp.finish(status=status, **attrs)

        # -- chain（run / step）------------------------------------------

        def on_chain_start(self, serialized: dict[str, Any], inputs: dict[str, Any],
                           *, run_id: UUID, parent_run_id: UUID | None = None,
                           **kwargs: Any) -> None:
            name = (serialized or {}).get("name") or "chain"
            base = RUN if parent_run_id is None else STEP
            self._open(run_id, parent_run_id, base, name)

        def on_chain_end(self, outputs: dict[str, Any], *, run_id: UUID,
                         parent_run_id: UUID | None = None, **kwargs: Any) -> None:
            self._close(run_id)
            if parent_run_id is None:
                self._adapter.root_span_id = None
                self._adapter.flush()

        def on_chain_error(self, error: BaseException, *, run_id: UUID,
                           parent_run_id: UUID | None = None, **kwargs: Any) -> None:
            self._on_error(error, run_id, parent_run_id)

        # -- LLM（model_call）-------------------------------------------

        def on_llm_start(self, serialized: dict[str, Any], prompts: list[str],
                         *, run_id: UUID, parent_run_id: UUID | None = None,
                         **kwargs: Any) -> None:
            name = (serialized or {}).get("name") or "llm"
            self._open(run_id, parent_run_id, MODEL_CALL, name, model=name)

        def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
            self._close(run_id, **_usage_attrs(response))

        def on_llm_error(self, error: BaseException, *, run_id: UUID,
                         parent_run_id: UUID | None = None, **kwargs: Any) -> None:
            self._on_error(error, run_id, parent_run_id)

        # -- tool（tool_call）------------------------------------------

        def on_tool_start(self, serialized: dict[str, Any], input_str: str,
                          *, run_id: UUID, parent_run_id: UUID | None = None,
                          **kwargs: Any) -> None:
            name = (serialized or {}).get("name") or "tool"
            self._open(run_id, parent_run_id, TOOL_CALL, name, tool_name=name)

        def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
            self._close(run_id, output_preview=str(output)[:200])

        def on_tool_error(self, error: BaseException, *, run_id: UUID,
                          parent_run_id: UUID | None = None, **kwargs: Any) -> None:
            self._on_error(error, run_id, parent_run_id)

        # -- retriever（retrieval）------------------------------------

        def on_retriever_start(self, serialized: dict[str, Any], query: str,
                               *, run_id: UUID, parent_run_id: UUID | None = None,
                               **kwargs: Any) -> None:
            name = (serialized or {}).get("name") or "retriever"
            self._open(run_id, parent_run_id, RETRIEVAL, name,
                       query_preview=str(query)[:120])

        def on_retriever_end(self, documents: Any, *, run_id: UUID,
                             **kwargs: Any) -> None:
            docs = list(documents or [])
            self._close(run_id, result_count=len(docs))

        def on_retriever_error(self, error: BaseException, *, run_id: UUID,
                               parent_run_id: UUID | None = None,
                               **kwargs: Any) -> None:
            self._on_error(error, run_id, parent_run_id)

        # -- 共用錯誤處理 -------------------------------------------------

        def _on_error(self, error: BaseException, run_id: UUID,
                      parent_run_id: UUID | None) -> None:
            sp = self._spans.get(run_id)
            parent = sp.span_id if sp else self._parent_span_id(parent_run_id)
            self._adapter.error(repr(error), parent=parent)
            self._close(run_id, status="error")

else:  # pragma: no cover - CI 無 langchain

    AnilaLangChainTracer = None  # type: ignore[assignment]


def _usage_attrs(response: Any) -> dict[str, Any]:
    """從 LangChain LLMResult 盡量抽 token usage（形狀因 provider 而異，容錯）。"""
    out = getattr(response, "llm_output", None) or {}
    usage = {}
    if isinstance(out, dict):
        usage = out.get("token_usage") or out.get("usage") or {}
    if not isinstance(usage, dict):
        return {}
    attrs: dict[str, Any] = {}
    for src, dst in (("prompt_tokens", "input_tokens"),
                     ("completion_tokens", "output_tokens"),
                     ("total_tokens", "total_tokens")):
        if src in usage:
            attrs[dst] = int(usage[src] or 0)
    return attrs


def _demo() -> None:
    """__main__ 示範樁：無 LangChain 時印安裝指引；有則印接法提示。"""
    if not _LANGCHAIN_AVAILABLE:
        print(_NOT_AVAILABLE_MSG)
        return
    adapter = adapter_from_env()
    tracer = AnilaLangChainTracer(adapter)  # type: ignore[operator]
    print("AnilaLangChainTracer 已就緒。把它加進 LangChain callbacks 即可：")
    print("  chain.invoke(inputs, config={'callbacks': [tracer]})")
    print(f"  adapter.active = {adapter.active}（缺 ANILA_TRACE_ID 時為 False）")
    _ = tracer  # 靜音未使用警告


if __name__ == "__main__":
    _demo()
