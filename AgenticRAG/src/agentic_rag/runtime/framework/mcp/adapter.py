"""Wrap MCP-exposed tools as framework ``Action`` objects.

One MCP server typically exposes 3-30 tools. Each one becomes a
framework ``Action`` whose handler dispatches back into the
``MCPClient.call_tool``. The Action's input_schema is whatever the
MCP server declared; the framework's input-validation middleware
applies normally before the call reaches the MCP server.

Namespace pattern:

  ``<server_name>__<tool_name>``

The double-underscore separator is unambiguous (most MCP tool names
use single underscores) and produces a name that matches what most
LLM-side tool routers accept (alphanumeric + underscore + hyphen).

The original (un-namespaced) tool name is preserved in the Action's
metadata so the handler can pass the right thing back to the MCP
session.
"""

from __future__ import annotations

import logging

from agentic_rag.runtime.framework.action import (
    Action,
    ActionContext,
    ActionKind,
    ActionResult,
    SideEffectClass,
)
from agentic_rag.runtime.framework.mcp.client import (
    MCPClient,
    MCPToolError,
    MCPToolMetadata,
)

logger = logging.getLogger(__name__)


# ── Namespacing ──────────────────────────────────────────────────────


_NAMESPACE_SEP = "__"


def namespaced_tool_name(server_name: str, tool_name: str) -> str:
    """Return ``server__tool`` — the framework-side name for an MCP tool."""
    return f"{server_name}{_NAMESPACE_SEP}{tool_name}"


def split_namespaced(name: str) -> tuple[str, str] | None:
    """Inverse of ``namespaced_tool_name``. ``None`` when the name has no namespace."""
    if _NAMESPACE_SEP not in name:
        return None
    parts = name.split(_NAMESPACE_SEP, 1)
    return parts[0], parts[1]


# ── Side-effect heuristic ────────────────────────────────────────────


_READ_HINTS = (
    "read", "list", "get", "fetch", "search", "find", "show",
    "view", "describe", "query", "scan",
)
_WRITE_HINTS = (
    "write", "create", "update", "delete", "remove", "set",
    "patch", "execute", "run", "send", "post",
)


def _infer_side_effect(name: str, description: str) -> SideEffectClass:
    """Best-effort side-effect classification from name + description.

    Heuristic only — author-defined Actions can pin this explicitly,
    but for auto-wrapped MCP tools we don't have author intent. The
    classification feeds tracing / cost dashboards; it's not a
    runtime gate.
    """
    haystack = (name + " " + description).lower()
    write_score = sum(1 for hint in _WRITE_HINTS if hint in haystack)
    read_score = sum(1 for hint in _READ_HINTS if hint in haystack)
    if write_score > read_score and write_score > 0:
        return SideEffectClass.NETWORKED
    if read_score > 0:
        return SideEffectClass.PURE
    # Unknown: assume networked (MCP servers are usually external systems)
    return SideEffectClass.NETWORKED


# ── Adapter ──────────────────────────────────────────────────────────


def mcp_tool_to_action(
    client: MCPClient,
    tool: MCPToolMetadata,
    *,
    namespaced: bool = True,
) -> Action:
    """Build a framework Action that proxies into the MCP client.

    ``namespaced=True`` (default) prefixes the Action name with the
    server name, preventing collisions when multiple MCP servers
    expose tools with the same bare name. Set False only when
    consuming a single server and you want naked tool names in the
    LLM prompt.

    The Action's handler:

    1. Calls ``client.call_tool(tool.name, ctx.params)``
    2. Wraps a successful result text as ``ActionResult(output=...)``
    3. Maps ``MCPToolError`` to a recoverable ``ActionResult.error``
       so the LLM sees the failure and can retry / give up rather
       than crashing the run
    4. Re-raises ``ConnectionError`` (transport down → run-level
       infra problem; pool-layer restart logic catches and re-spawns)
    """
    name = (
        namespaced_tool_name(client.server.name, tool.name)
        if namespaced
        else tool.name
    )
    description = tool.description or f"MCP tool {tool.name!r} on server {client.server.name!r}"
    side_effect = _infer_side_effect(tool.name, tool.description or "")

    async def _handler(ctx: ActionContext) -> ActionResult:
        try:
            text = await client.call_tool(tool.name, ctx.params)
        except MCPToolError as exc:
            return ActionResult(error=str(exc))
        # ConnectionError surfaces — pool layer decides whether to
        # restart the server. Keeping it as a raise lets the runner's
        # generic exception handler turn it into a tool error message
        # the LLM can react to.
        return ActionResult(output=text)

    return Action(
        name=name,
        description=description,
        kind=ActionKind.SYNC_TOOL,
        handler=_handler,
        input_schema=dict(tool.input_schema or {}),
        side_effect_class=side_effect,
    )


def all_actions_for_client(
    client: MCPClient,
    *,
    namespaced: bool = True,
) -> list[Action]:
    """Convenience: wrap every tool the connected client exposes.

    Caller must have already ``connect()``ed the client. Useful for
    one-line agent wiring::

        agent = Agent(
            actions=tuple(all_actions_for_client(my_mcp_client)),
            ...,
        )
    """
    return [
        mcp_tool_to_action(client, tool, namespaced=namespaced)
        for tool in client.tools
    ]


__all__ = [
    "all_actions_for_client",
    "mcp_tool_to_action",
    "namespaced_tool_name",
    "split_namespaced",
]
