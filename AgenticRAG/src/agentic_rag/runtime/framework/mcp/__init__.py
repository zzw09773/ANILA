"""MCP (Model Context Protocol) integration — consume third-party MCP servers.

ANILA's framework is an MCP **client**, not a server. We connect to
external MCP servers (filesystem, github, sentry, postgres, custom
ops scripts, …) over stdio and expose their tools as framework
``Action`` objects the LLM can call.

This package is opt-in via the ``[mcp]`` extra::

    pip install 'agentic-rag[mcp]'

Without it the modules below remain importable but instantiating any
class raises a clear ``UserError`` pointing at the install command.

What's here:

- ``client.MCPServer`` — declarative subprocess config (command/args/env)
- ``client.MCPClient`` — opens stdio session, lists tools, calls tools
- ``adapter.mcp_tool_to_action`` — wrap one MCP tool as a framework Action
- ``pool.MCPClientPool`` — manage N MCP servers concurrently with
  namespaced tool routing

Why stdio (and not HTTP/SSE) in v0.1: stdio is the most common MCP
transport, simplest to operate, no port management, no TLS. HTTP/SSE
support lands when a real fork needs it.

Why no MCP server hosting: ANILA wants to CONSUME external tooling.
Hosting our own MCP server (so other LLMs can call ANILA tools) is a
separate concern — much smaller surface, simpler to add later as a
dedicated module.
"""

from agentic_rag.runtime.framework.mcp.adapter import (
    mcp_tool_to_action,
    namespaced_tool_name,
)
from agentic_rag.runtime.framework.mcp.client import MCPClient, MCPServer
from agentic_rag.runtime.framework.mcp.pool import MCPClientPool

__all__ = [
    "MCPClient",
    "MCPClientPool",
    "MCPServer",
    "mcp_tool_to_action",
    "namespaced_tool_name",
]
