"""``MCPClient`` — stdio MCP subprocess + tool listing + tool calling.

Lifecycle:

1. ``client = MCPClient(MCPServer(command=...))`` — declares config; no I/O yet.
2. ``await client.connect()`` — spawns subprocess, opens stdio session,
   negotiates protocol, populates the cached tool list.
3. ``client.tools`` — read-only snapshot of MCP-exposed tools.
4. ``await client.call_tool(name, args)`` — round-trip RPC; returns
   the MCP CallToolResult (text content, structured payload, error).
5. ``await client.close()`` — shuts down the subprocess cleanly.

Or use as async context manager::

    async with MCPClient(server) as client:
        result = await client.call_tool("read_file", {"path": "/etc/hosts"})

Failure modes the client handles:

- mcp SDK not installed → UserError with install hint
- Subprocess won't start (binary missing / permission denied) →
  wrapped in UserError on connect()
- Subprocess crashes mid-session → next call_tool raises a clear
  ConnectionError; restart logic lives in the Pool layer
- Tool not found / call returns isError=True → mapped to
  ``MCPToolError`` carrying the server message

The client is not safe for concurrent ``call_tool`` from multiple
coroutines on the same instance — MCP's stdio session is
single-streamed. The Pool serialises calls per-client.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Optional

from agentic_rag.runtime.framework.exceptions import AgentsException, UserError

logger = logging.getLogger(__name__)


# ── Errors ───────────────────────────────────────────────────────────


class MCPToolError(AgentsException):
    """An MCP server returned isError=True or raised a protocol error."""

    server_name: str
    tool_name: str
    server_message: str

    def __init__(self, server_name: str, tool_name: str, server_message: str) -> None:
        self.server_name = server_name
        self.tool_name = tool_name
        self.server_message = server_message
        super().__init__(
            f"MCP server {server_name!r} tool {tool_name!r} failed: {server_message}"
        )


# ── Server config ────────────────────────────────────────────────────


@dataclass(frozen=True)
class MCPServer:
    """Declarative config for one MCP server subprocess.

    ``name`` is a short identifier used as the namespace prefix for
    this server's tools. Must be unique within an ``MCPClientPool``.

    ``env`` overrides / augments the parent process's environment for
    the subprocess. Use this to pass per-server secrets without
    leaking them to other tools.
    """

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.name.replace("_", "").isalnum():
            raise UserError(
                f"MCPServer.name must be alphanumeric / underscore only "
                f"(got {self.name!r}) — it's used as a tool-name prefix"
            )


# ── Tool metadata ────────────────────────────────────────────────────


@dataclass(frozen=True)
class MCPToolMetadata:
    """Snapshot of one MCP-exposed tool, captured at list_tools() time.

    Stored separately from the live mcp.types.Tool so consumers can
    survive subprocess restarts without losing the schema.
    """

    name: str
    description: str
    input_schema: dict[str, Any]


# ── Client ───────────────────────────────────────────────────────────


class MCPClient:
    """Single MCP-server client with explicit lifecycle.

    Two ways to use:

    Manual::

        client = MCPClient(server)
        await client.connect()
        try:
            result = await client.call_tool("...", {...})
        finally:
            await client.close()

    Context manager::

        async with MCPClient(server) as client:
            result = await client.call_tool(...)

    The connect / close sequence is idempotent — calling close on an
    already-closed client is a no-op. ``call_tool`` on a never-connected
    or already-closed client raises ConnectionError.
    """

    def __init__(self, server: MCPServer) -> None:
        # Lazy-import the SDK so installations without the [mcp] extra
        # only break when an MCPClient is actually instantiated.
        try:
            from mcp import StdioServerParameters
            from mcp.client.session import ClientSession
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise UserError(
                "MCPClient requires the 'mcp' package. "
                "Install with: pip install 'agentic-rag[mcp]'"
            ) from exc

        self._server = server
        self._stdio_client = stdio_client
        self._session_cls = ClientSession
        self._stdio_params = StdioServerParameters(
            command=server.command,
            args=list(server.args),
            env=server.env or None,
            cwd=server.cwd,
        )

        self._exit_stack: Optional[AsyncExitStack] = None
        self._session: Any = None  # mcp.ClientSession when connected
        self._tools: list[MCPToolMetadata] = []
        self._connected = False

    # ── Properties ────────────────────────────────────────────────────

    @property
    def server(self) -> MCPServer:
        return self._server

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def tools(self) -> list[MCPToolMetadata]:
        """Snapshot of tools the connected server exposes.

        Returns the cached list captured during connect() — does not
        refetch. Servers that dynamically grow / shrink their tool list
        require an explicit ``await client.refresh_tools()`` call.
        """
        return list(self._tools)

    # ── Lifecycle ────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Spawn the subprocess + open the MCP session + cache tools.

        Idempotent: a second call on an already-connected client is a
        no-op. Failures wind down cleanly — partial state from a
        crashed connect() never leaks into a next attempt.
        """
        if self._connected:
            return
        self._exit_stack = AsyncExitStack()
        try:
            read, write = await self._exit_stack.enter_async_context(
                self._stdio_client(self._stdio_params)
            )
            self._session = await self._exit_stack.enter_async_context(
                self._session_cls(read, write)
            )
            await self._session.initialize()
            await self.refresh_tools()
            self._connected = True
        except Exception as exc:
            await self._cleanup_on_error()
            raise UserError(
                f"MCPClient {self._server.name!r}: connect failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    async def close(self) -> None:
        """Tear down the session + subprocess. Idempotent."""
        if not self._connected and self._exit_stack is None:
            return
        try:
            if self._exit_stack is not None:
                await self._exit_stack.aclose()
        except Exception:  # noqa: BLE001
            logger.exception(
                "MCPClient %s: close raised; subprocess may have crashed",
                self._server.name,
            )
        finally:
            self._connected = False
            self._exit_stack = None
            self._session = None
            self._tools = []

    async def __aenter__(self) -> "MCPClient":
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        await self.close()

    # ── Tool surface ─────────────────────────────────────────────────

    async def refresh_tools(self) -> list[MCPToolMetadata]:
        """Re-fetch the server's tool list. Returns the new snapshot."""
        self._require_connected()
        try:
            response = await self._session.list_tools()
        except Exception as exc:  # noqa: BLE001
            raise ConnectionError(
                f"MCPClient {self._server.name!r}: list_tools failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        self._tools = [
            MCPToolMetadata(
                name=t.name,
                description=t.description or "",
                input_schema=dict(t.inputSchema or {}),
            )
            for t in response.tools
        ]
        return list(self._tools)

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Call an MCP tool and return the result text.

        MCP CallToolResult contains a list of content blocks (text /
        image / resource). For framework Action consumption we
        concatenate text blocks and ignore non-text. Callers needing
        the raw result should drop down to ``call_tool_raw``.

        Raises ``MCPToolError`` when the server flags ``isError=True``;
        raises ``ConnectionError`` on transport-level failure.
        """
        result = await self.call_tool_raw(tool_name, arguments)
        # Aggregate text content; ignore image / resource blocks.
        parts: list[str] = []
        for block in result.content or []:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
        text_result = "\n".join(parts).strip()
        if getattr(result, "isError", False):
            raise MCPToolError(
                self._server.name, tool_name, text_result or "MCP server returned isError=True"
            )
        return text_result

    async def call_tool_raw(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Lower-level call returning the SDK's raw CallToolResult."""
        self._require_connected()
        try:
            return await self._session.call_tool(tool_name, arguments=arguments)
        except Exception as exc:  # noqa: BLE001
            raise ConnectionError(
                f"MCPClient {self._server.name!r}: call_tool {tool_name!r} failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    # ── Helpers ──────────────────────────────────────────────────────

    def _require_connected(self) -> None:
        if not self._connected:
            raise ConnectionError(
                f"MCPClient {self._server.name!r}: not connected. "
                "Call await client.connect() first or use the async-with context."
            )

    async def _cleanup_on_error(self) -> None:
        """Best-effort teardown when connect() fails partway through."""
        if self._exit_stack is not None:
            try:
                await self._exit_stack.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._exit_stack = None
        self._session = None
        self._tools = []
        self._connected = False


__all__ = ["MCPClient", "MCPServer", "MCPToolError", "MCPToolMetadata"]
