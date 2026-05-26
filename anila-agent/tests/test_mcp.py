"""P1-5 MCP 整合單元測試。

涵蓋範圍:
- :class:`MCPServer` ABC 不可直接 instantiate。
- :class:`MCPServerStdio` 用 fake stream(模擬 subprocess stdio)走 JSON-RPC handshake
  + tools/list + tools/call。
- :class:`MCPServerStreamableHttp` 用 ``httpx.MockTransport`` 模擬 HTTP server。
- :class:`MCPServerSse` 用 ``prepared_frames`` 注入 path。
- :class:`MCPServerManager` 的 lifecycle(start_all / stop_all)、tool 動態註冊、
  on_mcp_server_connect / disconnect callback。
- YAML loader 從 example 檔載入正確的 server class。

所有測試刻意不依賴真實 MCP server,以避免 PyPI / 網路依賴。
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any

import httpx
import pytest

from anila_agent.core.events import EventBus
from anila_agent.mcp import (
    MCPServer,
    MCPServerManager,
    MCPServerSse,
    MCPServerStdio,
    MCPServerStreamableHttp,
    MCPTool,
    MCPTransportError,
    load_mcp_servers_from_yaml,
)
from anila_agent.mcp.manager import _MCP_TOOL_METADATA
from anila_agent.tools.base import get_metadata
from anila_agent.tools.registry import ToolRegistry
from anila_agent.tracing import Tracer

# ---------------------------------------------------------------------------
# Fakes — 模擬 subprocess stdio
# ---------------------------------------------------------------------------


class _FakeReader:
    """async readline supplier。每個 line 為一筆已序列化的 JSON-RPC frame(byte string)。"""

    def __init__(self) -> None:
        self.lines: deque[bytes] = deque()

    def feed_frame(self, frame: dict[str, Any]) -> None:
        self.lines.append((json.dumps(frame) + "\n").encode("utf-8"))

    def feed_raw(self, line: bytes) -> None:
        self.lines.append(line)

    async def readline(self) -> bytes:
        if not self.lines:
            # 模擬 EOF
            return b""
        return self.lines.popleft()


class _FakeWriter:
    """同步 write + async drain — 把寫出的 frame 解析後存進 sent。"""

    def __init__(self, reader: _FakeReader | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._reader = reader
        # auto_responder:每次收到帶 id 的 request,自動 push 對應 response 到 reader。
        self.auto_responder: dict[str, Any] | None = None

    def write(self, data: bytes) -> None:
        text = data.decode("utf-8").strip()
        if not text:
            return
        try:
            frame = json.loads(text)
        except json.JSONDecodeError:
            return
        self.sent.append(frame)
        if self.auto_responder is not None and self._reader is not None and "id" in frame:
            method = frame["method"]
            responder = self.auto_responder.get(method)
            if responder is not None:
                response = {
                    "jsonrpc": "2.0",
                    "id": frame["id"],
                    "result": responder(frame.get("params", {})),
                }
                self._reader.feed_frame(response)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------


def test_mcp_server_abc_cannot_be_instantiated() -> None:
    """:class:`MCPServer` ABC 直接 instantiate 必須拋 TypeError。"""
    with pytest.raises(TypeError):
        MCPServer(name="x")  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# MCPServerStdio
# ---------------------------------------------------------------------------


def _stdio_factory(
    initialize_result: dict[str, Any] | None = None,
    tools: list[dict[str, Any]] | None = None,
    call_result: Any = None,
) -> tuple[_FakeReader, _FakeWriter]:
    """組好一對 reader/writer + auto-responder,涵蓋 handshake + tools/list + tools/call。"""
    reader = _FakeReader()
    writer = _FakeWriter(reader=reader)
    writer.auto_responder = {
        "initialize": lambda _params: initialize_result
        or {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "fake", "version": "0.1"},
            "capabilities": {},
        },
        "tools/list": lambda _params: {"tools": tools or []},
        "tools/call": lambda _params: {
            "isError": False,
            "content": call_result if call_result is not None else [{"type": "text", "text": "ok"}],
        },
    }
    return reader, writer


@pytest.mark.asyncio
async def test_stdio_handshake_and_tools_flow() -> None:
    """stdio transport 應完成 handshake(initialize + initialized notification),
    然後 tools/list 與 tools/call 都能往返。
    """
    tools_payload = [
        {
            "name": "read_file",
            "description": "Read a file",
            "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]
    reader, writer = _stdio_factory(tools=tools_payload, call_result="file contents")

    server = MCPServerStdio(
        name="fs",
        transport_factory=(reader, writer),
    )
    await server.connect()
    assert server.is_connected is True

    # handshake:第一個 frame 必為 initialize request,接著是 initialized notification。
    assert writer.sent[0]["method"] == "initialize"
    assert writer.sent[1]["method"] == "notifications/initialized"
    assert "id" not in writer.sent[1]  # notification 不帶 id

    tools = await server.list_tools()
    assert len(tools) == 1
    assert tools[0].name == "read_file"
    assert tools[0].server_name == "fs"
    assert tools[0].input_schema["properties"]["path"]["type"] == "string"

    result = await server.call_tool("read_file", {"path": "/tmp/x"})
    assert result == "file contents"

    await server.disconnect()
    assert server.is_connected is False
    assert writer.closed is True


@pytest.mark.asyncio
async def test_stdio_disconnect_idempotent() -> None:
    reader, writer = _stdio_factory()
    server = MCPServerStdio(name="fs", transport_factory=(reader, writer))
    await server.connect()
    await server.disconnect()
    # 再次呼叫不該爆。
    await server.disconnect()


@pytest.mark.asyncio
async def test_stdio_require_connected_before_call() -> None:
    server = MCPServerStdio(name="fs", command="echo")
    with pytest.raises(MCPTransportError):
        await server.list_tools()


@pytest.mark.asyncio
async def test_stdio_handles_server_isError_response() -> None:
    """tools/call 收到 ``isError: true`` 時應抛 :class:`MCPTransportError`。"""
    reader = _FakeReader()
    writer = _FakeWriter(reader=reader)
    writer.auto_responder = {
        "initialize": lambda _p: {"protocolVersion": "2024-11-05", "serverInfo": {}, "capabilities": {}},
        "tools/list": lambda _p: {"tools": []},
        "tools/call": lambda _p: {"isError": True, "content": "blew up"},
    }
    server = MCPServerStdio(name="fs", transport_factory=(reader, writer))
    await server.connect()
    with pytest.raises(MCPTransportError):
        await server.call_tool("x", {})


@pytest.mark.asyncio
async def test_stdio_skips_unrelated_notifications() -> None:
    """server 在 response 之前可能先送一筆 notification(沒 id),client 應跳過繼續等。"""
    reader = _FakeReader()
    writer = _FakeWriter(reader=reader)
    writer.auto_responder = {
        "initialize": lambda _p: {"protocolVersion": "2024-11-05", "serverInfo": {}, "capabilities": {}},
        "tools/list": lambda _p: {"tools": []},
    }
    server = MCPServerStdio(name="fs", transport_factory=(reader, writer))
    await server.connect()
    # 先塞一筆 notification,再讓 auto_responder 補上正常 response 時應該被跳過。
    reader.feed_frame({"jsonrpc": "2.0", "method": "log", "params": {"msg": "hi"}})
    tools = await server.list_tools()
    assert tools == []


# ---------------------------------------------------------------------------
# MCPServerStreamableHttp via MockTransport
# ---------------------------------------------------------------------------


def _http_handler_factory(
    tools_payload: list[dict[str, Any]] | None = None,
    call_payload: Any = None,
) -> httpx.MockTransport:
    """造一個 httpx.MockTransport,自動依 method 給出對應的 JSON-RPC response。"""

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method = body.get("method")
        # notification(沒 id)— server 用 202 回 ack,body 為空。
        if "id" not in body:
            return httpx.Response(202, content=b"")
        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {"name": "fake", "version": "0.1"},
                        "capabilities": {},
                    },
                },
            )
        if method == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"tools": tools_payload or []},
                },
            )
        if method == "tools/call":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "isError": False,
                        "content": call_payload if call_payload is not None else "ok",
                    },
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(_handler)


@pytest.mark.asyncio
async def test_streamable_http_handshake_and_tools_flow() -> None:
    """streamable HTTP transport 走完整流程。"""
    tools_payload = [
        {
            "name": "list_repos",
            "description": "List repos",
            "inputSchema": {"type": "object"},
        }
    ]
    transport = _http_handler_factory(tools_payload=tools_payload, call_payload="repo1,repo2")
    server = MCPServerStreamableHttp(
        name="gh",
        url="https://example.com/mcp/",
        transport=transport,
    )
    await server.connect()
    assert server.is_connected is True

    tools = await server.list_tools()
    assert len(tools) == 1
    assert tools[0].name == "list_repos"
    assert tools[0].server_name == "gh"

    out = await server.call_tool("list_repos", {})
    assert out == "repo1,repo2"

    await server.disconnect()


@pytest.mark.asyncio
async def test_streamable_http_http_error_propagates() -> None:
    """HTTP 4xx/5xx 應該 raise :class:`MCPTransportError`。"""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    server = MCPServerStreamableHttp(
        name="gh",
        url="https://example.com/mcp/",
        transport=httpx.MockTransport(_handler),
    )
    with pytest.raises(MCPTransportError):
        await server.connect()


# ---------------------------------------------------------------------------
# MCPServerSse — prepared_frames path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_prepared_frames_flow() -> None:
    """SSE transport 透過 ``prepared_frames`` 注入 — 不開真 HTTP 連線。"""
    prepared = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {},
                "capabilities": {},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo",
                        "inputSchema": {"type": "object"},
                    }
                ]
            },
        },
    ]
    server = MCPServerSse(name="se", url="http://x", prepared_frames=prepared)
    await server.connect()

    tools = await server.list_tools()
    assert [t.name for t in tools] == ["echo"]
    await server.disconnect()


# ---------------------------------------------------------------------------
# MCPServerManager — lifecycle + 動態 tool 註冊
# ---------------------------------------------------------------------------


class _FakeServer(MCPServer):
    """測試用 server — 直接傳入 tools / call_result,跳過 transport。"""

    def __init__(
        self,
        name: str,
        tools: list[MCPTool],
        call_result: Any = "fake-result",
    ) -> None:
        super().__init__(name=name)
        self._tools = tools
        self._call_result = call_result
        self.opened = False
        self.closed = False

    async def _open_transport(self) -> None:
        self.opened = True

    async def _close_transport(self) -> None:
        self.closed = True

    async def _send_raw(self, payload: dict[str, Any]) -> None:
        return None

    async def _receive_raw(self) -> dict[str, Any]:
        return {}

    async def connect(self) -> None:
        # 跳過 handshake,直接標記 connected。
        if self.is_connected:
            return
        await self._open_transport()
        self._transport_ready = True
        self._connected = True

    async def disconnect(self) -> None:
        if not self._connected and not self._transport_ready:
            return
        await self._close_transport()
        self._transport_ready = False
        self._connected = False

    async def list_tools(self) -> list[MCPTool]:
        return list(self._tools)

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        return self._call_result


@pytest.mark.asyncio
async def test_manager_start_all_registers_tools_with_metadata() -> None:
    """manager.start_all 應該把每個 server 的 tool 註冊進 ``ToolRegistry``,
    並附上 P0-2 metadata (cost_estimate=medium / is_open_world=True / category=mcp)。
    """
    registry = ToolRegistry()
    bus = EventBus()
    manager = MCPServerManager(registry=registry, event_bus=bus)

    server_a = _FakeServer(
        "alpha",
        tools=[
            MCPTool(name="a_tool", description="A tool", input_schema={"type": "object"}, server_name="alpha"),
        ],
    )
    server_b = _FakeServer(
        "beta",
        tools=[
            MCPTool(name="b_tool_1", description="B tool 1", input_schema={"type": "object"}, server_name="beta"),
            MCPTool(name="b_tool_2", description="B tool 2", input_schema={"type": "object"}, server_name="beta"),
        ],
    )
    manager.add(server_a)
    manager.add(server_b)

    captured_events: list[str] = []
    bus.on_any(lambda e: captured_events.append(e.kind) if e.kind.startswith("mcp_") else None)

    await manager.start_all()

    # registry 多了 3 個 tool。
    assert "mcp__alpha__a_tool" in registry.tools
    assert "mcp__beta__b_tool_1" in registry.tools
    assert "mcp__beta__b_tool_2" in registry.tools

    # metadata 都是 MCP 預設。
    meta = registry.get_metadata("mcp__alpha__a_tool")
    assert meta.cost_estimate == "medium"
    assert meta.is_open_world is True
    assert meta.category == "mcp"

    # connect 事件 emit 到 bus。
    assert captured_events.count("mcp_server_connect") == 2


@pytest.mark.asyncio
async def test_manager_stop_all_unregisters_tools_and_closes() -> None:
    """stop_all 應 unregister 所有動態 tool,並 disconnect 每個 server。"""
    registry = ToolRegistry()
    bus = EventBus()
    manager = MCPServerManager(registry=registry, event_bus=bus)

    server = _FakeServer(
        "alpha",
        tools=[MCPTool("t1", "T1", {"type": "object"}, server_name="alpha")],
    )
    manager.add(server)
    await manager.start_all()
    assert "mcp__alpha__t1" in registry.tools

    captured_events: list[str] = []
    bus.on_any(lambda e: captured_events.append(e.kind) if e.kind.startswith("mcp_") else None)

    await manager.stop_all()
    assert "mcp__alpha__t1" not in registry.tools
    assert server.closed is True
    assert "mcp_server_disconnect" in captured_events


@pytest.mark.asyncio
async def test_manager_lifecycle_callbacks_fire() -> None:
    """``on_mcp_server_connect`` / ``on_mcp_server_disconnect`` callback 應被呼叫。"""
    registry = ToolRegistry()
    manager = MCPServerManager(registry=registry)

    connect_log: list[str] = []
    disconnect_log: list[str] = []
    manager.on_mcp_server_connect(lambda name: connect_log.append(name))
    manager.on_mcp_server_disconnect(lambda name: disconnect_log.append(name))

    manager.add(
        _FakeServer(
            "alpha",
            tools=[MCPTool("t", "T", {"type": "object"}, server_name="alpha")],
        )
    )

    await manager.start_all()
    assert connect_log == ["alpha"]
    await manager.stop_all()
    assert disconnect_log == ["alpha"]


@pytest.mark.asyncio
async def test_manager_call_tool_traces_span() -> None:
    """tool 呼叫應在 tracer 上開一個 ``mcp.call.<server>.<tool>`` span (P0-9)。"""

    class _RecordingProcessor:
        def __init__(self) -> None:
            self.spans: list[Any] = []

        def on_trace_start(self, trace: Any) -> None:
            pass

        def on_trace_end(self, trace: Any) -> None:
            pass

        def on_span_start(self, span: Any) -> None:
            self.spans.append(span)

        def on_span_end(self, span: Any) -> None:
            pass

    registry = ToolRegistry()
    tracer = Tracer()
    proc = _RecordingProcessor()
    tracer.register_processor(proc)  # type: ignore[arg-type]

    manager = MCPServerManager(registry=registry, tracer=tracer)
    manager.add(
        _FakeServer(
            "alpha",
            tools=[MCPTool("t", "T", {"type": "object"}, server_name="alpha")],
            call_result="hello",
        )
    )
    await manager.start_all()

    with tracer.start_trace("test"):
        result = await manager.call_tool("alpha", "t", {})
    assert result == "hello"

    span_names = [s.name for s in proc.spans]
    assert "mcp.call.alpha.t" in span_names
    span = next(s for s in proc.spans if s.name == "mcp.call.alpha.t")
    assert span.attributes["mcp.server"] == "alpha"
    assert span.attributes["mcp.tool"] == "t"

    await manager.stop_all()


@pytest.mark.asyncio
async def test_manager_call_tool_unknown_server_raises() -> None:
    manager = MCPServerManager(registry=ToolRegistry())
    with pytest.raises(KeyError):
        await manager.call_tool("missing", "x", {})


def test_manager_add_duplicate_name_raises() -> None:
    manager = MCPServerManager(registry=ToolRegistry())
    manager.add(_FakeServer("dup", tools=[]))
    with pytest.raises(ValueError):
        manager.add(_FakeServer("dup", tools=[]))


@pytest.mark.asyncio
async def test_function_tool_built_from_mcp_invokes_call_tool() -> None:
    """``build_function_tool_from_mcp`` 包出來的 :class:`FunctionTool`
    被 invoke 時應透過 manager 走 ``call_tool``,並回傳序列化字串。
    """
    registry = ToolRegistry()
    manager = MCPServerManager(registry=registry)
    manager.add(
        _FakeServer(
            "alpha",
            tools=[MCPTool("t", "T", {"type": "object"}, server_name="alpha")],
            call_result={"items": [1, 2, 3]},
        )
    )
    await manager.start_all()

    fn_tool = registry.tools["mcp__alpha__t"]
    # FunctionTool.on_invoke_tool(ctx, args_str) — args_str 是 JSON encoded
    result = await fn_tool.on_invoke_tool(None, '{"foo": "bar"}')  # type: ignore[arg-type]
    assert result == json.dumps({"items": [1, 2, 3]}, ensure_ascii=False)

    # 同時驗 metadata 保留。
    meta = get_metadata(fn_tool)
    assert meta == _MCP_TOOL_METADATA

    await manager.stop_all()


# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------


_EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "anila_agent" / "mcp" / "examples"


def test_load_filesystem_mcp_yaml() -> None:
    servers = load_mcp_servers_from_yaml(_EXAMPLE_DIR / "filesystem_mcp.yaml")
    assert len(servers) == 1
    server = servers[0]
    assert isinstance(server, MCPServerStdio)
    assert server.name == "filesystem"
    assert server.command == "npx"
    assert "@modelcontextprotocol/server-filesystem" in server.args


def test_load_github_mcp_yaml() -> None:
    servers = load_mcp_servers_from_yaml(_EXAMPLE_DIR / "github_mcp.yaml")
    assert len(servers) == 1
    server = servers[0]
    assert isinstance(server, MCPServerStreamableHttp)
    assert server.name == "github"
    assert server.url.startswith("https://")
    assert server.headers.get("Authorization", "").startswith("Bearer")


def test_load_yaml_unsupported_transport_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "servers:\n  - name: x\n    transport: telnet\n    url: foo\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_mcp_servers_from_yaml(bad)


def test_load_yaml_missing_name_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("servers:\n  - transport: stdio\n    command: echo\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_mcp_servers_from_yaml(bad)
