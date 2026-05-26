"""MCP server manager — 註冊多個 :class:`MCPServer`、統一 lifecycle、橋接動態 tool 註冊。

整合點:

- **P0-1 lifecycle hooks**:server connect / disconnect 時 fire 自訂事件
  (``mcp_server_connect`` / ``mcp_server_disconnect``)到 event bus,並支援
  ``on_mcp_server_connect`` / ``on_mcp_server_disconnect`` callback 註冊。
- **P0-2 tool metadata**:每個 MCP-provided tool 在註冊進 ``ToolRegistry`` 時都
  附上 metadata ``cost_estimate="medium"``、``is_open_world=True``、
  ``category="mcp"``,讓 hook / guardrail 可以辨識。
- **P0-9 tracing**:tool 呼叫包在 ``tracer.start_span("mcp.call.<server>.<tool>")``
  內,自動帶 ``mcp.server`` / ``mcp.tool`` attribute。

Manager 是 stateless 工具:lifecycle 各步驟由呼叫端(通常為 ``AnilaRunner.start``
/ ``stop``)在適當時機觸發。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from agents import FunctionTool

from anila_agent.core.events import EventBus
from anila_agent.mcp.server import (
    MCPServer,
    MCPServerSse,
    MCPServerStdio,
    MCPServerStreamableHttp,
    MCPTool,
)
from anila_agent.tools.base import ToolMetadata, _attach_metadata
from anila_agent.tools.registry import ToolRegistry
from anila_agent.tracing import Tracer

logger = logging.getLogger(__name__)


# MCP-provided tool 的預設 metadata — 套到每個動態註冊的 tool 上。
_MCP_TOOL_METADATA = ToolMetadata(
    is_read_only=False,
    is_destructive=False,
    cost_estimate="medium",
    requires_approval=False,
    is_open_world=True,
    category="mcp",
)

# Lifecycle callback alias。
MCPLifecycleCallback = Callable[[str], Awaitable[None] | None]


@dataclass
class _RegisteredEntry:
    """manager 內部追蹤 — 同時保留 server 本體與其註冊的 tool name list,
    讓 disconnect 時可以正確 unregister。"""

    server: MCPServer
    tool_names: list[str] = field(default_factory=list)


@dataclass
class MCPServerManager:
    """集中管理多個 :class:`MCPServer`。

    典型用法:

        manager = MCPServerManager(registry=tool_registry, event_bus=bus)
        manager.add(MCPServerStdio(name="fs", command="...", args=[...]))
        manager.on_mcp_server_connect(lambda name: log(f"{name} up"))

        await manager.start_all()    # 連線 + 註冊 tool
        # ... agent 運作 ...
        await manager.stop_all()     # 斷線 + unregister tool

    Attributes:
        registry: tool registry。MCP-provided tool 會動態 add/remove。
        event_bus: 可選 event bus,connect/disconnect 事件會 emit 到這。
        tracer: 可選 tracer,P0-9 span 用。
    """

    registry: ToolRegistry
    event_bus: EventBus | None = None
    tracer: Tracer | None = None

    def __post_init__(self) -> None:
        self._entries: dict[str, _RegisteredEntry] = {}
        self._on_connect: list[MCPLifecycleCallback] = []
        self._on_disconnect: list[MCPLifecycleCallback] = []

    # ------------------------------------------------------------------
    # registration
    # ------------------------------------------------------------------

    def add(self, server: MCPServer) -> None:
        """加入一個 MCP server。重複 name 會拋錯。"""
        if server.name in self._entries:
            raise ValueError(f"Duplicate MCP server name: {server.name!r}")
        self._entries[server.name] = _RegisteredEntry(server=server)

    def add_many(self, servers: Iterable[MCPServer]) -> None:
        for s in servers:
            self.add(s)

    def get(self, name: str) -> MCPServer:
        """取已註冊的 server,找不到拋 KeyError。"""
        return self._entries[name].server

    @property
    def server_names(self) -> list[str]:
        return list(self._entries.keys())

    def on_mcp_server_connect(self, cb: MCPLifecycleCallback) -> None:
        """註冊 server connect 完成後要跑的 callback(P0-1 lifecycle hook)。"""
        self._on_connect.append(cb)

    def on_mcp_server_disconnect(self, cb: MCPLifecycleCallback) -> None:
        """註冊 server disconnect 時要跑的 callback(P0-1 lifecycle hook)。"""
        self._on_disconnect.append(cb)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def start_all(self) -> None:
        """依序連所有 server,並把 server-side tool 註冊進 registry。

        失敗時保留前面已連的 server,只 raise 該 server 的錯。呼叫端應視情況決定
        是否回滾(可呼叫 :meth:`stop_all`)。
        """
        for name, entry in self._entries.items():
            await self._connect_one(entry)
            logger.info("MCP server %s connected, %d tools registered", name, len(entry.tool_names))

    async def stop_all(self) -> None:
        """依反向順序斷開所有 server。即使某個 server disconnect 失敗,也會繼續處理其他 server。

        Tool unregister 在 server disconnect 之前執行(避免 LLM 又把已死 server 的
        tool 拿來打)。
        """
        for name, entry in reversed(list(self._entries.items())):
            await self._disconnect_one(entry)
            logger.info("MCP server %s disconnected", name)

    async def list_all_tools(self) -> list[MCPTool]:
        """聚合所有 connected server 的 tool 清單(已連線者才會列入)。"""
        out: list[MCPTool] = []
        for entry in self._entries.values():
            if not entry.server.is_connected:
                continue
            tools = await entry.server.list_tools()
            out.extend(tools)
        return out

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        args: dict[str, Any],
    ) -> Any:
        """呼叫指定 server 的指定 tool。P0-9 tracing span 在這層包好。"""
        if server_name not in self._entries:
            raise KeyError(f"MCP server {server_name!r} not registered")
        server = self._entries[server_name].server
        if self.tracer is not None:
            with self.tracer.start_span(
                f"mcp.call.{server_name}.{tool_name}",
                attributes={"mcp.server": server_name, "mcp.tool": tool_name},
            ):
                return await server.call_tool(tool_name, args)
        return await server.call_tool(tool_name, args)

    # ------------------------------------------------------------------
    # 內部:per-server lifecycle
    # ------------------------------------------------------------------

    async def _connect_one(self, entry: _RegisteredEntry) -> None:
        server = entry.server
        await server.connect()
        if self.event_bus is not None:
            self.event_bus.emit("mcp_server_connect", server=server.name)
        await self._fire_lifecycle(self._on_connect, server.name)
        # 拉一次 tool list 並註冊到 registry。
        try:
            tools = await server.list_tools()
        except Exception:
            # list_tools 失敗時讓 connect 流程整個倒帶。
            await server.disconnect()
            raise
        for mcp_tool in tools:
            fn_tool = build_function_tool_from_mcp(self, mcp_tool)
            self.registry.add(fn_tool)
            entry.tool_names.append(fn_tool.name)

    async def _disconnect_one(self, entry: _RegisteredEntry) -> None:
        # 先 unregister tool,避免外面 race。
        for tool_name in entry.tool_names:
            self.registry.tools.pop(tool_name, None)
        entry.tool_names.clear()
        server = entry.server
        try:
            await server.disconnect()
        except Exception:
            logger.exception("MCP server %s disconnect failed; swallowed", server.name)
        if self.event_bus is not None:
            self.event_bus.emit("mcp_server_disconnect", server=server.name)
        await self._fire_lifecycle(self._on_disconnect, server.name)

    async def _fire_lifecycle(
        self,
        callbacks: list[MCPLifecycleCallback],
        server_name: str,
    ) -> None:
        for cb in callbacks:
            try:
                ret = cb(server_name)
                if asyncio.iscoroutine(ret):
                    await ret
            except Exception:
                logger.exception(
                    "MCP lifecycle callback for %s raised; swallowed", server_name
                )


# ---------------------------------------------------------------------------
# Tool 包裝
# ---------------------------------------------------------------------------


def build_function_tool_from_mcp(
    manager: MCPServerManager,
    mcp_tool: MCPTool,
) -> FunctionTool:
    """把 :class:`MCPTool` 包成 openai-agents :class:`FunctionTool`,附 Anila metadata。

    包名格式:``mcp__<server>__<tool>``,避免與本機 tool 撞名。
    """
    qualified_name = f"mcp__{mcp_tool.server_name}__{mcp_tool.name}"

    async def _invoke(_ctx: Any, args_str: str) -> Any:
        # FunctionTool callback 收到的是 JSON-encoded args(openai-agents 規格)。
        try:
            parsed: dict[str, Any] = json.loads(args_str) if args_str else {}
        except json.JSONDecodeError:
            parsed = {}
        result = await manager.call_tool(mcp_tool.server_name, mcp_tool.name, parsed)
        # LLM 想看的是字串 — 若 server 給 list/dict,序列化即可。
        if isinstance(result, str):
            return result
        try:
            return json.dumps(result, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(result)

    schema = dict(mcp_tool.input_schema) if mcp_tool.input_schema else {"type": "object"}
    # openai-agents 預設要 strict JSON schema(additionalProperties: false / required 必填)。
    # MCP server 給的 schema 多半沒這樣設,所以這裡關掉 strict 以兼容。
    fn_tool = FunctionTool(
        name=qualified_name,
        description=mcp_tool.description or f"MCP tool {mcp_tool.name} from {mcp_tool.server_name}",
        params_json_schema=schema,
        on_invoke_tool=_invoke,
        strict_json_schema=False,
    )
    return _attach_metadata(fn_tool, _MCP_TOOL_METADATA)


# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------


def load_mcp_servers_from_yaml(path: str | Path) -> list[MCPServer]:
    """從 yaml 設定檔載入 MCP server 設定 → 對應的 :class:`MCPServer` instance。

    YAML 格式範例:

        servers:
          - name: filesystem
            transport: stdio
            command: npx
            args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
          - name: github
            transport: http
            url: https://api.githubcopilot.com/mcp/
            headers:
              Authorization: Bearer ${GITHUB_PAT}

    支援的 transport:``stdio`` / ``sse`` / ``http``。Env-var 展開不在這層做(目的:
    保持載入純函式無 side effect);呼叫端可在 yaml 解開前自行做 string.format。
    """
    path = Path(path)
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"MCP yaml at {path} must be a mapping, got {type(data).__name__}")
    raw_servers = data.get("servers", [])
    if not isinstance(raw_servers, list):
        raise ValueError(f"MCP yaml at {path}: 'servers' must be a list")

    out: list[MCPServer] = []
    for entry in raw_servers:
        if not isinstance(entry, dict):
            continue
        server = _build_server_from_yaml_entry(entry)
        out.append(server)
    return out


def _build_server_from_yaml_entry(entry: dict[str, Any]) -> MCPServer:
    """依 yaml entry 的 ``transport`` 欄位分派到正確的 server class。"""
    name = str(entry.get("name") or "")
    if not name:
        raise ValueError(f"MCP server entry missing 'name': {entry!r}")
    transport = str(entry.get("transport") or "").lower()

    if transport == "stdio":
        command = str(entry.get("command") or "")
        if not command:
            raise ValueError(f"MCP stdio server {name!r} missing 'command'")
        args_raw = entry.get("args") or []
        if not isinstance(args_raw, list):
            raise ValueError(f"MCP stdio server {name!r} 'args' must be a list")
        env_raw = entry.get("env")
        env_typed: dict[str, str] | None = None
        if env_raw is not None:
            if not isinstance(env_raw, dict):
                raise ValueError(f"MCP stdio server {name!r} 'env' must be a mapping")
            env_typed = {str(k): str(v) for k, v in env_raw.items()}
        return MCPServerStdio(
            name=name,
            command=command,
            args=[str(a) for a in args_raw],
            env=env_typed,
            cwd=str(entry["cwd"]) if entry.get("cwd") else None,
        )

    if transport == "sse":
        url = str(entry.get("url") or "")
        if not url:
            raise ValueError(f"MCP sse server {name!r} missing 'url'")
        headers_raw = entry.get("headers") or {}
        return MCPServerSse(
            name=name,
            url=url,
            headers={str(k): str(v) for k, v in headers_raw.items()},
            timeout=float(entry.get("timeout") or 30.0),
        )

    if transport == "http":
        url = str(entry.get("url") or "")
        if not url:
            raise ValueError(f"MCP http server {name!r} missing 'url'")
        headers_raw = entry.get("headers") or {}
        return MCPServerStreamableHttp(
            name=name,
            url=url,
            headers={str(k): str(v) for k, v in headers_raw.items()},
            timeout=float(entry.get("timeout") or 30.0),
        )

    raise ValueError(
        f"MCP server {name!r} has unsupported transport {transport!r}; "
        f"expected one of stdio / sse / http"
    )
