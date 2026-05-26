"""anila-agent MCP sub-package — Model Context Protocol 整合 (P1-5)。

MCP (Model Context Protocol) 是 Anthropic 制定的 server-client 協定,讓 agent 可以
動態接 external tool server。本 sub-package 實作三種 transport(stdio / SSE /
streamable-HTTP),並由 :class:`MCPServerManager` 統一管理 lifecycle:

    agent 啟動 → 連線所有 MCP server → 把 server-side tool 註冊進 ToolRegistry
    agent 結束 → 斷線並 unregister 動態 tool

設計考量:
- 不引入 ``mcp`` PyPI 套件 — ANILA on-prem 環境可能無 PyPI 連線,因此自行
  實作極小的 JSON-RPC 2.0 client(只用標準函式庫 + 既有 ``httpx``)。
- MCP 每個 tool 都假設為 open-world(會打外部資源)且成本 medium,自動套
  ``cost_estimate="medium"`` / ``is_open_world=True`` metadata。
- 與既有 lifecycle hook (P0-1)、tool metadata (P0-2)、tracing (P0-9) 整合,
  分別在 connect/disconnect、tool 註冊、tool 呼叫各埋一個 hook event /
  registry write / tracing span。

公開介面:
    MCPServer           — base ABC,所有 transport 的共同介面。
    MCPServerStdio      — subprocess + JSON-RPC over stdin/stdout。
    MCPServerSse        — JSON-RPC over httpx Server-Sent Events。
    MCPServerStreamableHttp — JSON-RPC over httpx streamable HTTP。
    MCPServerManager    — 多 server 註冊 / lifecycle / 動態 tool 註冊。
    MCPTool             — server-side tool 描述 dataclass。
    load_mcp_servers_from_yaml — 從 yaml 檔載入 server 設定。
"""

from __future__ import annotations

from anila_agent.mcp.manager import (
    MCPServerManager,
    build_function_tool_from_mcp,
    load_mcp_servers_from_yaml,
)
from anila_agent.mcp.server import (
    MCPServer,
    MCPServerSse,
    MCPServerStdio,
    MCPServerStreamableHttp,
    MCPTool,
    MCPTransportError,
)

__all__ = [
    "MCPServer",
    "MCPServerManager",
    "MCPServerSse",
    "MCPServerStdio",
    "MCPServerStreamableHttp",
    "MCPTool",
    "MCPTransportError",
    "build_function_tool_from_mcp",
    "load_mcp_servers_from_yaml",
]
