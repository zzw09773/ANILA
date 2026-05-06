"""Tool registry. Resolves tool names to FunctionTool instances and de-duplicates."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from agents import FunctionTool

from anila_agent.tools.base import import_tool


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


def load_tools(qualified_paths: Iterable[str]) -> list[FunctionTool]:
    """Resolve qualified attribute paths to FunctionTools.

    A registry is used so duplicate names (e.g. accidental double-registration via
    config + code) fail loudly rather than silently shadowing one another.
    """
    registry = ToolRegistry()
    for path in qualified_paths:
        registry.add(import_tool(path))
    return registry.as_list()
