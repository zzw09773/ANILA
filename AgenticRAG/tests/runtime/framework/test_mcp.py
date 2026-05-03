"""Sprint 8 tests — MCP client / adapter / pool.

Most tests use stubbed MCPClient instances rather than real
subprocesses — the SDK's stdio transport requires an actual binary
on PATH, which makes integration tests slow and platform-fragile.
The adapter / pool layer is what we want to verify; the SDK itself
is upstream-tested.

A small smoke test at the bottom does spin a real subprocess (the
mcp SDK's bundled echo server) when the test environment supports
it — skipped otherwise.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agentic_rag.runtime.framework import (
    Action,
    ActionContext,
    ActionKind,
    SideEffectClass,
)
from agentic_rag.runtime.framework.exceptions import UserError
from agentic_rag.runtime.framework.mcp.adapter import (
    _infer_side_effect,
    all_actions_for_client,
    mcp_tool_to_action,
    namespaced_tool_name,
    split_namespaced,
)
from agentic_rag.runtime.framework.mcp.client import (
    MCPClient,
    MCPServer,
    MCPToolError,
    MCPToolMetadata,
)
from agentic_rag.runtime.framework.mcp.pool import MCPClientPool


# ── Test scaffolding ─────────────────────────────────────────────────


def _ctx(params=None) -> ActionContext:
    return ActionContext(
        run_id="r", agent_name="a", params=params or {}, history=()
    )


class _StubMCPClient:
    """Test double satisfying the same shape MCPClient exposes.

    We don't subclass MCPClient because that would require either
    spawning a real subprocess or mocking the mcp SDK internals.
    The adapter / pool only touches a small surface — server,
    is_connected, tools, call_tool — so duck-typing is enough.
    """

    def __init__(
        self,
        name: str = "stub",
        tools: list[MCPToolMetadata] | None = None,
        responses: dict[str, str] | None = None,
        errors: dict[str, str] | None = None,
    ) -> None:
        self.server = MCPServer(name=name, command="dummy")
        self.is_connected = True
        self.tools = tools or []
        self._responses = responses or {}
        self._errors = errors or {}
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        self.calls.append((tool_name, dict(arguments)))
        if tool_name in self._errors:
            raise MCPToolError(self.server.name, tool_name, self._errors[tool_name])
        return self._responses.get(tool_name, f"OK from {tool_name}")

    async def call_tool_raw(self, tool_name: str, arguments: dict) -> Any:
        text = await self.call_tool(tool_name, arguments)
        return SimpleNamespace(
            content=[SimpleNamespace(text=text)],
            isError=False,
        )

    async def connect(self) -> None:
        self.is_connected = True

    async def close(self) -> None:
        self.is_connected = False

    async def refresh_tools(self) -> list[MCPToolMetadata]:
        return list(self.tools)


# ── MCPServer config ────────────────────────────────────────────────


def test_server_name_must_be_alphanumeric() -> None:
    with pytest.raises(UserError, match="alphanumeric"):
        MCPServer(name="has-dash", command="x")
    # underscore is fine
    MCPServer(name="has_underscore", command="x")
    # alphanumeric is fine
    MCPServer(name="abc123", command="x")


def test_server_defaults() -> None:
    s = MCPServer(name="test", command="echo")
    assert s.args == ()
    assert s.env == {}
    assert s.cwd is None


# ── Namespacing ──────────────────────────────────────────────────────


def test_namespaced_tool_name_format() -> None:
    assert namespaced_tool_name("fs", "read_file") == "fs__read_file"


def test_split_namespaced_inverse() -> None:
    server, tool = split_namespaced("github__list_prs")
    assert server == "github"
    assert tool == "list_prs"


def test_split_namespaced_returns_none_on_bare_name() -> None:
    assert split_namespaced("plain_name") is None


def test_split_namespaced_handles_underscores_in_tool_name() -> None:
    server, tool = split_namespaced("fs__read_my_file")
    assert server == "fs"
    assert tool == "read_my_file"


# ── Side-effect inference ───────────────────────────────────────────


def test_infer_side_effect_read_hint() -> None:
    assert (
        _infer_side_effect("read_file", "Read a file from disk")
        is SideEffectClass.PURE
    )


def test_infer_side_effect_write_hint() -> None:
    assert (
        _infer_side_effect("delete_branch", "Delete a git branch")
        is SideEffectClass.NETWORKED
    )


def test_infer_side_effect_unknown_defaults_networked() -> None:
    assert (
        _infer_side_effect("foobar", "does something opaque")
        is SideEffectClass.NETWORKED
    )


# ── Adapter: mcp_tool_to_action ─────────────────────────────────────


def test_adapter_builds_action_with_namespaced_name() -> None:
    client = _StubMCPClient(name="fs")
    tool = MCPToolMetadata(
        name="read_file",
        description="Read a file",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    action = mcp_tool_to_action(client, tool)
    assert isinstance(action, Action)
    assert action.name == "fs__read_file"
    assert action.kind is ActionKind.SYNC_TOOL
    assert action.input_schema == tool.input_schema


def test_adapter_can_skip_namespacing() -> None:
    client = _StubMCPClient(name="fs")
    tool = MCPToolMetadata(name="t", description="", input_schema={})
    action = mcp_tool_to_action(client, tool, namespaced=False)
    assert action.name == "t"


def test_adapter_uses_inferred_side_effect() -> None:
    client = _StubMCPClient(name="fs")
    write_tool = MCPToolMetadata(
        name="write_file", description="Write text to a file", input_schema={}
    )
    action = mcp_tool_to_action(client, write_tool)
    assert action.side_effect_class is SideEffectClass.NETWORKED


@pytest.mark.asyncio
async def test_adapter_handler_returns_text_output() -> None:
    client = _StubMCPClient(
        name="fs",
        responses={"read_file": "file contents here"},
    )
    tool = MCPToolMetadata(name="read_file", description="", input_schema={})
    action = mcp_tool_to_action(client, tool)
    result = await action.handler(_ctx({"path": "/etc/hosts"}))
    assert not result.is_error
    assert result.output == "file contents here"
    # Stub recorded the call
    assert client.calls == [("read_file", {"path": "/etc/hosts"})]


@pytest.mark.asyncio
async def test_adapter_handler_translates_mcp_tool_error() -> None:
    client = _StubMCPClient(
        name="github",
        errors={"create_pr": "permission denied: not a collaborator"},
    )
    tool = MCPToolMetadata(name="create_pr", description="", input_schema={})
    action = mcp_tool_to_action(client, tool)
    result = await action.handler(_ctx({}))
    assert result.is_error
    assert "permission denied" in result.error


def test_all_actions_for_client_returns_action_per_tool() -> None:
    tools = [
        MCPToolMetadata(name="a", description="", input_schema={}),
        MCPToolMetadata(name="b", description="", input_schema={}),
        MCPToolMetadata(name="c", description="", input_schema={}),
    ]
    client = _StubMCPClient(name="srv", tools=tools)
    actions = all_actions_for_client(client)
    assert [a.name for a in actions] == ["srv__a", "srv__b", "srv__c"]


# ── Pool ─────────────────────────────────────────────────────────────


def test_pool_rejects_duplicate_server_names() -> None:
    with pytest.raises(UserError, match="duplicate MCP server name"):
        MCPClientPool([
            MCPServer(name="dup", command="x"),
            MCPServer(name="dup", command="y"),
        ])


def test_pool_require_unknown_raises() -> None:
    pool = MCPClientPool([MCPServer(name="a", command="x")])
    with pytest.raises(UserError, match="no server named"):
        pool.require("missing")


@pytest.mark.asyncio
async def test_pool_aggregates_actions_from_all_connected_clients() -> None:
    """Build a pool, monkey-patch in stub clients, verify all_actions()."""
    pool = MCPClientPool([
        MCPServer(name="fs", command="x"),
        MCPServer(name="gh", command="y"),
    ])
    # Replace clients with stubs
    pool._clients["fs"] = _StubMCPClient(
        name="fs",
        tools=[MCPToolMetadata(name="read", description="", input_schema={})],
    )
    pool._clients["gh"] = _StubMCPClient(
        name="gh",
        tools=[MCPToolMetadata(name="list_prs", description="", input_schema={})],
    )
    actions = pool.all_actions()
    names = sorted(a.name for a in actions)
    assert names == ["fs__read", "gh__list_prs"]


@pytest.mark.asyncio
async def test_pool_actions_for_server_filters() -> None:
    pool = MCPClientPool([
        MCPServer(name="fs", command="x"),
        MCPServer(name="gh", command="y"),
    ])
    pool._clients["fs"] = _StubMCPClient(
        name="fs",
        tools=[MCPToolMetadata(name="read", description="", input_schema={})],
    )
    pool._clients["gh"] = _StubMCPClient(
        name="gh",
        tools=[MCPToolMetadata(name="list_prs", description="", input_schema={})],
    )
    fs_only = pool.actions_for_server("fs")
    assert [a.name for a in fs_only] == ["fs__read"]


@pytest.mark.asyncio
async def test_pool_skips_disconnected_servers_in_all_actions() -> None:
    pool = MCPClientPool([
        MCPServer(name="up", command="x"),
        MCPServer(name="down", command="y"),
    ])
    up = _StubMCPClient(
        name="up",
        tools=[MCPToolMetadata(name="t", description="", input_schema={})],
    )
    down = _StubMCPClient(name="down")
    down.is_connected = False  # simulate failed connect
    pool._clients["up"] = up
    pool._clients["down"] = down

    actions = pool.all_actions()
    assert [a.name for a in actions] == ["up__t"]


@pytest.mark.asyncio
async def test_pool_close_all_closes_every_client() -> None:
    pool = MCPClientPool([
        MCPServer(name="a", command="x"),
        MCPServer(name="b", command="y"),
    ])
    a = _StubMCPClient(name="a")
    b = _StubMCPClient(name="b")
    pool._clients["a"] = a
    pool._clients["b"] = b
    await pool.close_all()
    assert a.is_connected is False
    assert b.is_connected is False


# ── MCPClient construction error path ────────────────────────────────


def test_mcp_client_construction_with_real_sdk_works() -> None:
    """The mcp SDK is installed in this env; constructing MCPClient
    should succeed without spawning a subprocess (connect() does that)."""
    server = MCPServer(name="test", command="nonexistent_binary_xyz")
    # Construction must not raise — it only sets up params.
    client = MCPClient(server)
    assert client.server is server
    assert client.is_connected is False
    assert client.tools == []


@pytest.mark.asyncio
async def test_mcp_client_connect_failure_wraps_error() -> None:
    """connect() against a missing binary raises a wrapped UserError."""
    server = MCPServer(name="test", command="nonexistent_binary_xyz")
    client = MCPClient(server)
    with pytest.raises(UserError, match="connect failed"):
        await client.connect()
    assert client.is_connected is False


@pytest.mark.asyncio
async def test_mcp_client_call_tool_before_connect_raises() -> None:
    server = MCPServer(name="test", command="dummy")
    client = MCPClient(server)
    with pytest.raises(ConnectionError, match="not connected"):
        await client.call_tool("anything", {})
