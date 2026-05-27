"""Anila tool wrapper around openai-agents `@function_tool`.

擴充 claude-code-src `Tool` 介面在 Python 端的對應 metadata,讓 hook、guardrail、
concurrency partitioner 都能基於 tool 屬性決策。

設計參考:
- claude-code-src `src/Tool.ts`:`isReadOnly` / `isDestructive` / `isConcurrencySafe`
  / `isOpenWorld` / `alwaysLoad` / `shouldDefer` 等屬性。
- openai-agents `function_tool` decorator:既有 schema 推導機制保留不動。

提供兩種使用方式:
- 裝飾器:`@anila_tool(is_read_only=True, ...)`
- 程式化:`AnilaTool.from_function(fn, is_read_only=True, ...)`
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from agents import FunctionTool, function_tool

# 粗略成本標記。`None` 代表未標註,呼叫端應視為「未知」。
CostEstimate = Literal["free", "low", "medium", "high"]


@dataclass(frozen=True)
class ToolMetadata:
    """Anila tool 的 metadata。由 hook、guardrail、registry 查詢使用,不會送進 LLM prompt。

    所有欄位皆有預設值,既有未宣告 metadata 的 tool 仍可正常運作(向後相容)。

    Attributes:
        is_read_only: 是否唯讀。`True` 代表 tool 不會改變外部狀態,可平行執行、可快取。
        is_destructive: 是否具破壞性(不可逆操作,例如刪檔、改 DB)。`True` 時 guardrail
            與 approval policy 必須先檢查。
        concurrency_safe: 是否可平行執行。預設值依 `is_read_only` 推導 — 唯讀 tool 預設
            可平行,寫入 tool 預設不可。可顯式覆寫(例如 idempotent 寫入)。
            注意:此欄位透過 `__post_init__` 在 `None` 時依 `is_read_only` 自動推導。
        cost_estimate: 粗略成本標記,供 cost-aware router / budget tracker 使用。
            `None` 代表未標註。
        requires_approval: 是否需要使用者明確 approve 才能執行。對應 claude-code-src
            的 permission flow。
        is_open_world: 是否需要對外網路(例如呼叫外部 API、爬網頁)。`True` 時 policy
            可拒絕在 offline / sandbox 環境執行。
        category: tool 分類字串(例如 "filesystem" / "retrieval" / "compute"),供
            registry 過濾與 UI 分組使用。
        is_deferred: P1-18 deferred tool 標記。`True` 代表 tool 預設「不放進 prompt」,
            只有 LLM 透過 ``tool_search`` meta-tool 找到並 ``activate_tool`` 啟用後
            才會出現在 active list。對應 claude-code-src `Tool.shouldDefer`。
            MCP-provided tool 預設值為 True(對應 `Tool.isMcp`),其他 tool 預設 False。
        requires_confirmation: 保留欄位 — 舊版 alias,與 `requires_approval` 語意相同。
            為避免破壞既有呼叫端,兩個欄位都保留。新 code 請優先使用 `requires_approval`。
    """

    is_read_only: bool = False
    is_destructive: bool = False
    concurrency_safe: bool | None = None
    cost_estimate: CostEstimate | None = None
    requires_approval: bool = False
    is_open_world: bool = False
    category: str = "general"
    is_deferred: bool = False
    # 舊欄位,保留以維持向後相容(P0-2 前的呼叫端會傳這個 keyword)。
    requires_confirmation: bool = False

    def __post_init__(self) -> None:
        # `concurrency_safe` 未顯式宣告時依 `is_read_only` 推導:
        #   唯讀 tool → 預設可平行;寫入 tool → 預設不可平行。
        # 使用 `object.__setattr__` 繞過 frozen dataclass 限制。
        if self.concurrency_safe is None:
            object.__setattr__(self, "concurrency_safe", self.is_read_only)
        # `requires_confirmation` 與 `requires_approval` 互通,任一為 True 都生效。
        if self.requires_confirmation and not self.requires_approval:
            object.__setattr__(self, "requires_approval", True)


_METADATA_ATTR = "__anila_metadata__"


def _attach_metadata(tool: FunctionTool, metadata: ToolMetadata) -> FunctionTool:
    setattr(tool, _METADATA_ATTR, metadata)
    return tool


def get_metadata(tool: FunctionTool | Any) -> ToolMetadata:
    """回傳 tool 的 Anila metadata;未宣告者回傳預設值。

    向後相容用 — 舊 tool 沒 metadata 也能跑,呼叫端拿到的是 `ToolMetadata()` 預設值。
    """
    meta = getattr(tool, _METADATA_ATTR, None)
    if isinstance(meta, ToolMetadata):
        return meta
    return ToolMetadata()


def anila_tool(
    *,
    is_read_only: bool = False,
    is_destructive: bool = False,
    concurrency_safe: bool | None = None,
    cost_estimate: CostEstimate | None = None,
    requires_approval: bool = False,
    is_open_world: bool = False,
    category: str = "general",
    is_deferred: bool = False,
    requires_confirmation: bool = False,
    **function_tool_kwargs: Any,
) -> Callable[[Callable[..., Any]], FunctionTool]:
    """`@function_tool` + Anila metadata 的裝飾器。

    用法:

        @anila_tool(is_read_only=True, category="retrieval", cost_estimate="low")
        def search_documents(query: str, k: int = 5) -> list[dict]: ...

    Args:
        is_read_only: 唯讀 tool 標記。
        is_destructive: 破壞性 tool 標記。
        concurrency_safe: 是否可平行;`None` 表示依 `is_read_only` 自動推導。
        cost_estimate: 成本標記("free" / "low" / "medium" / "high")。
        requires_approval: 是否需 user approval。
        is_open_world: 是否需對外網路。
        category: tool 分類。
        is_deferred: P1-18 deferred 標記;True 代表此 tool 預設不放進 prompt,
            由 ``tool_search`` / ``activate_tool`` 動態啟用。
        requires_confirmation: 舊欄位 alias,與 `requires_approval` 等義。
        **function_tool_kwargs: 透傳給 openai-agents `function_tool`(例如
            `name_override` / `description_override`)。
    """

    def decorator(fn: Callable[..., Any]) -> FunctionTool:
        wrapped = function_tool(**function_tool_kwargs)(fn)
        if not isinstance(wrapped, FunctionTool):
            raise TypeError("function_tool did not return a FunctionTool")
        return _attach_metadata(
            wrapped,
            ToolMetadata(
                is_read_only=is_read_only,
                is_destructive=is_destructive,
                concurrency_safe=concurrency_safe,
                cost_estimate=cost_estimate,
                requires_approval=requires_approval,
                is_open_world=is_open_world,
                category=category,
                is_deferred=is_deferred,
                requires_confirmation=requires_confirmation,
            ),
        )

    return decorator


@dataclass
class AnilaTool:
    """非裝飾器路徑的程式化 tool builder。

    用於 function 來自 runtime bound(例如 class method、closure)而不便用裝飾器的情境。
    """

    fn: Callable[..., Any]
    metadata: ToolMetadata = field(default_factory=ToolMetadata)
    name: str | None = None
    description: str | None = None

    def build(self) -> FunctionTool:
        kwargs: dict[str, Any] = {}
        if self.name is not None:
            kwargs["name_override"] = self.name
        if self.description is not None:
            kwargs["description_override"] = self.description
        wrapped = function_tool(**kwargs)(self.fn)
        if not isinstance(wrapped, FunctionTool):
            raise TypeError("function_tool did not return a FunctionTool")
        return _attach_metadata(wrapped, self.metadata)

    @classmethod
    def from_function(
        cls,
        fn: Callable[..., Any],
        *,
        is_read_only: bool = False,
        is_destructive: bool = False,
        concurrency_safe: bool | None = None,
        cost_estimate: CostEstimate | None = None,
        requires_approval: bool = False,
        is_open_world: bool = False,
        category: str = "general",
        is_deferred: bool = False,
        requires_confirmation: bool = False,
        name: str | None = None,
        description: str | None = None,
    ) -> FunctionTool:
        """以 keyword 直接組 metadata 並包成 FunctionTool。"""
        return cls(
            fn=fn,
            metadata=ToolMetadata(
                is_read_only=is_read_only,
                is_destructive=is_destructive,
                concurrency_safe=concurrency_safe,
                cost_estimate=cost_estimate,
                requires_approval=requires_approval,
                is_open_world=is_open_world,
                category=category,
                is_deferred=is_deferred,
                requires_confirmation=requires_confirmation,
            ),
            name=name,
            description=description,
        ).build()


@functools.lru_cache(maxsize=128)
def _import_attribute(qualified: str) -> Any:
    import importlib

    module_path, _, attr = qualified.rpartition(".")
    if not module_path:
        raise ValueError(f"Invalid import path: {qualified!r}")
    return getattr(importlib.import_module(module_path), attr)


def import_tool(qualified: str) -> FunctionTool:
    """以 fully-qualified attribute path 載入 FunctionTool。"""
    obj = _import_attribute(qualified)
    if isinstance(obj, FunctionTool):
        return obj
    if callable(obj):
        return AnilaTool.from_function(obj)
    raise TypeError(f"{qualified!r} is neither a FunctionTool nor callable")
