"""Tool registry — 將 tool name 對應到 FunctionTool 並去重。

P0-2 後支援 metadata 查詢:
    registry.find_by_metadata(is_read_only=True)
    registry.find_by_metadata(category="filesystem", is_destructive=True)

供 hook / guardrail / concurrency partitioner 過濾 tool 用。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from agents import FunctionTool

from anila_agent.tools.base import CostEstimate, ToolMetadata, get_metadata, import_tool


@dataclass
class ToolRegistry:
    tools: dict[str, FunctionTool] = field(default_factory=dict)

    def add(self, tool: FunctionTool) -> None:
        if tool.name in self.tools:
            raise ValueError(f"Duplicate tool name: {tool.name!r}")
        self.tools[tool.name] = tool

    def add_many(self, tools: Iterable[FunctionTool]) -> None:
        for t in tools:
            self.add(t)

    def as_list(self) -> list[FunctionTool]:
        return list(self.tools.values())

    def get_metadata(self, name: str) -> ToolMetadata:
        """回傳指定 tool 的 metadata;未註冊則 raise KeyError。"""
        if name not in self.tools:
            raise KeyError(f"Tool not in registry: {name!r}")
        return get_metadata(self.tools[name])

    def find_by_metadata(
        self,
        *,
        is_read_only: bool | None = None,
        is_destructive: bool | None = None,
        concurrency_safe: bool | None = None,
        cost_estimate: CostEstimate | None = None,
        requires_approval: bool | None = None,
        is_open_world: bool | None = None,
        category: str | None = None,
    ) -> list[FunctionTool]:
        """依 metadata 條件過濾 tool。

        `None` 表示不過濾該欄位;非 `None` 則需精準匹配。多個條件以 AND 串接。

        範例:
            # 所有唯讀且屬於 retrieval 類別的 tool
            registry.find_by_metadata(is_read_only=True, category="retrieval")

            # 所有破壞性 tool(供 guardrail 列管)
            registry.find_by_metadata(is_destructive=True)
        """
        filters: dict[str, Any] = {
            "is_read_only": is_read_only,
            "is_destructive": is_destructive,
            "concurrency_safe": concurrency_safe,
            "cost_estimate": cost_estimate,
            "requires_approval": requires_approval,
            "is_open_world": is_open_world,
            "category": category,
        }
        active = {k: v for k, v in filters.items() if v is not None}

        result: list[FunctionTool] = []
        for tool in self.tools.values():
            meta = get_metadata(tool)
            if all(getattr(meta, k) == v for k, v in active.items()):
                result.append(tool)
        return result


def load_tools(qualified_paths: Iterable[str]) -> list[FunctionTool]:
    """以 fully-qualified attribute path 解析 FunctionTool。

    透過 registry 去重,重複 tool name(例如 config + code 雙重註冊)會直接失敗
    而非默默覆蓋。
    """
    registry = ToolRegistry()
    for path in qualified_paths:
        registry.add(import_tool(path))
    return registry.as_list()
