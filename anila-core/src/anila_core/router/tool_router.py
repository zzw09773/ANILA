"""Tool Router — registry, permission enforcement, and batch execution.

The ToolRegistry holds tool definitions and enforces allow/deny rules.
execute_batch runs concurrency-safe tools in parallel, others sequentially.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from ..models.message import ToolCall, ToolResult
from ..models.tool import ToolDefinition, ToolSafety


class RouterError(Exception):
    """Raised for tool routing configuration errors."""


class ToolRegistry:
    """Holds ToolDefinition records and resolves execution permissions.

    Permission rules (applied in order):
      1. deny_list: explicit denials take precedence
      2. allow_list: if present, only listed tools are allowed
      3. Wildcard "*" in allow_list allows everything not in deny_list

    If allow_list is empty, all tools not in deny_list are allowed.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._allow_list: list[str] = []
        self._deny_list: list[str] = []

    def register(self, definition: ToolDefinition) -> None:
        """Register a tool definition."""
        self._tools[definition.name] = definition

    def set_allow_list(self, tools: list[str]) -> None:
        """Restrict execution to this list. Supports "*" wildcard."""
        self._allow_list = list(tools)

    def set_deny_list(self, tools: list[str]) -> None:
        """Explicitly deny these tools."""
        self._deny_list = list(tools)

    def can_use(self, tool_name: str) -> bool:
        """Return True if the tool may be executed given current rules."""
        if tool_name in self._deny_list:
            return False
        if not self._allow_list:
            return True
        if "*" in self._allow_list:
            return True
        return tool_name in self._allow_list

    def get(self, tool_name: str) -> ToolDefinition:
        """Return a registered ToolDefinition.

        Raises RouterError if not found.
        """
        if tool_name not in self._tools:
            available = sorted(self._tools.keys())
            raise RouterError(
                f"Unknown tool '{tool_name}'. Available: {available}"
            )
        return self._tools[tool_name]

    def get_or_none(self, tool_name: str) -> Optional[ToolDefinition]:
        """Return a registered ToolDefinition or None."""
        return self._tools.get(tool_name)

    def list_tools(self) -> list[str]:
        """Return names of all registered tools."""
        return sorted(self._tools.keys())

    def openai_schemas(self, tool_names: Optional[list[str]] = None) -> list[dict]:
        """Generate OpenAI-compatible tool schemas.

        If tool_names is provided, only include those tools.
        """
        names = tool_names if tool_names is not None else self.list_tools()
        schemas = []
        for name in names:
            tool = self._tools.get(name)
            if tool:
                schemas.append(tool.to_openai_schema())
        return schemas

    def anthropic_schemas(self, tool_names: Optional[list[str]] = None) -> list[dict]:
        """Generate Anthropic-compatible tool schemas."""
        names = tool_names if tool_names is not None else self.list_tools()
        schemas = []
        for name in names:
            tool = self._tools.get(name)
            if tool:
                schemas.append(tool.to_anthropic_schema())
        return schemas

    async def execute(
        self,
        call: ToolCall,
        context: Optional[dict[str, Any]] = None,
    ) -> ToolResult:
        """Execute a single tool call.

        Returns a ToolResult. On permission denial or missing implementation,
        returns an error ToolResult rather than raising.
        """
        if not self.can_use(call.name):
            return ToolResult(
                tool_call_id=call.id,
                content=f"Tool '{call.name}' is not permitted in this context.",
                is_error=True,
            )

        tool = self.get_or_none(call.name)
        if tool is None:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Unknown tool: '{call.name}'",
                is_error=True,
            )

        if tool.implementation is None:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Tool '{call.name}' has no implementation registered.",
                is_error=True,
            )

        try:
            if asyncio.iscoroutinefunction(tool.implementation):
                raw = await tool.implementation(call.input, **(context or {}))
            else:
                raw = tool.implementation(call.input, **(context or {}))

            # Normalize result to string
            if isinstance(raw, str):
                content = raw
            elif isinstance(raw, (dict, list)):
                import json
                content = json.dumps(raw, ensure_ascii=False)
            else:
                content = str(raw)

            return ToolResult(tool_call_id=call.id, content=content)

        except PermissionError as exc:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Permission denied: {exc}",
                is_error=True,
            )
        except TimeoutError as exc:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Tool timed out: {exc}",
                is_error=True,
            )
        except Exception as exc:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Tool error: {exc}",
                is_error=True,
            )


async def execute_batch(
    registry: ToolRegistry,
    calls: list[ToolCall],
    context: Optional[dict[str, Any]] = None,
) -> list[ToolResult]:
    """Execute a batch of tool calls with concurrency awareness.

    Concurrency-safe tools (ToolSafety.CONCURRENCY_SAFE) are gathered in
    parallel. All others execute sequentially in order.

    This matches Claude Code's pattern: READ_ONLY / DESTRUCTIVE tools run
    one-at-a-time to avoid hidden state corruption.
    """
    if not calls:
        return []

    results: list[ToolResult] = []
    pending_concurrent: list[ToolCall] = []

    async def flush_concurrent() -> None:
        """Execute all accumulated concurrent calls in parallel."""
        if not pending_concurrent:
            return
        batch_results = await asyncio.gather(
            *[registry.execute(c, context) for c in pending_concurrent]
        )
        results.extend(batch_results)
        pending_concurrent.clear()

    for call in calls:
        tool = registry.get_or_none(call.name)
        is_concurrent = (
            tool is not None
            and tool.safety == ToolSafety.CONCURRENCY_SAFE
            and registry.can_use(call.name)
        )

        if is_concurrent:
            pending_concurrent.append(call)
        else:
            # Flush any accumulated concurrent calls first
            await flush_concurrent()
            result = await registry.execute(call, context)
            results.append(result)

    # Flush final batch of concurrent calls
    await flush_concurrent()

    return results
