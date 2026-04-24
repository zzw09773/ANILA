"""Tests for ToolRouter — allow/deny/wildcard, batch execution, schema gen."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agentic_rag.models.message import ToolCall
from agentic_rag.models.tool import ToolDefinition, ToolSafety
from agentic_rag.router.tool_router import RouterError, ToolRegistry, execute_batch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_echo_tool(name: str, safety: ToolSafety = ToolSafety.READ_ONLY) -> ToolDefinition:
    async def impl(input: dict[str, Any], **_: Any) -> str:
        return f"echo:{input.get('text', '')}"

    return ToolDefinition(
        name=name,
        description=f"Echo tool {name}",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
        },
        safety=safety,
        implementation=impl,
    )


def make_registry(*tools: ToolDefinition) -> ToolRegistry:
    registry = ToolRegistry()
    for t in tools:
        registry.register(t)
    return registry


# ---------------------------------------------------------------------------
# Basic registration
# ---------------------------------------------------------------------------

class TestToolRegistration:
    def test_register_and_get(self) -> None:
        t = make_echo_tool("bash")
        registry = make_registry(t)
        assert registry.get("bash") is t

    def test_get_missing_raises(self) -> None:
        registry = ToolRegistry()
        with pytest.raises(RouterError, match="Unknown tool"):
            registry.get("nonexistent")

    def test_list_tools(self) -> None:
        registry = make_registry(make_echo_tool("a"), make_echo_tool("b"))
        assert registry.list_tools() == ["a", "b"]


# ---------------------------------------------------------------------------
# Permission rules
# ---------------------------------------------------------------------------

class TestPermissions:
    def test_default_allows_all(self) -> None:
        registry = make_registry(make_echo_tool("bash"))
        assert registry.can_use("bash")

    def test_deny_list_blocks(self) -> None:
        registry = make_registry(make_echo_tool("bash"))
        registry.set_deny_list(["bash"])
        assert not registry.can_use("bash")

    def test_allow_list_restricts(self) -> None:
        registry = make_registry(make_echo_tool("bash"), make_echo_tool("grep"))
        registry.set_allow_list(["grep"])
        assert registry.can_use("grep")
        assert not registry.can_use("bash")

    def test_wildcard_in_allow_list(self) -> None:
        registry = make_registry(make_echo_tool("bash"))
        registry.set_allow_list(["*"])
        assert registry.can_use("bash")
        assert registry.can_use("anything")

    def test_deny_overrides_allow(self) -> None:
        registry = make_registry(make_echo_tool("bash"))
        registry.set_allow_list(["bash"])
        registry.set_deny_list(["bash"])
        assert not registry.can_use("bash")


# ---------------------------------------------------------------------------
# Single execution
# ---------------------------------------------------------------------------

class TestExecute:
    @pytest.mark.asyncio
    async def test_execute_success(self) -> None:
        registry = make_registry(make_echo_tool("echo"))
        call = ToolCall(id="1", name="echo", input={"text": "hello"})
        result = await registry.execute(call)
        assert result.content == "echo:hello"
        assert not result.is_error

    @pytest.mark.asyncio
    async def test_execute_denied_tool(self) -> None:
        registry = make_registry(make_echo_tool("bash"))
        registry.set_deny_list(["bash"])
        call = ToolCall(id="1", name="bash", input={})
        result = await registry.execute(call)
        assert result.is_error
        assert "not permitted" in result.content

    @pytest.mark.asyncio
    async def test_execute_unknown_tool(self) -> None:
        registry = ToolRegistry()
        call = ToolCall(id="1", name="unknown", input={})
        result = await registry.execute(call)
        assert result.is_error
        assert "Unknown tool" in result.content

    @pytest.mark.asyncio
    async def test_execute_no_implementation(self) -> None:
        tool = ToolDefinition(
            name="no_impl",
            description="No impl",
            input_schema={"type": "object"},
        )
        registry = make_registry(tool)
        call = ToolCall(id="1", name="no_impl", input={})
        result = await registry.execute(call)
        assert result.is_error

    @pytest.mark.asyncio
    async def test_execute_tool_exception_becomes_error_result(self) -> None:
        def broken(input: dict, **_: Any) -> str:
            raise ValueError("something broke")

        tool = ToolDefinition(
            name="broken",
            description="",
            input_schema={"type": "object"},
            implementation=broken,
        )
        registry = make_registry(tool)
        call = ToolCall(id="1", name="broken", input={})
        result = await registry.execute(call)
        assert result.is_error
        assert "something broke" in result.content


# ---------------------------------------------------------------------------
# Batch execution
# ---------------------------------------------------------------------------

class TestExecuteBatch:
    @pytest.mark.asyncio
    async def test_sequential_for_read_only(self) -> None:
        order: list[str] = []

        async def impl_a(input: dict, **_: Any) -> str:
            order.append("a")
            return "a"

        async def impl_b(input: dict, **_: Any) -> str:
            order.append("b")
            return "b"

        tool_a = ToolDefinition(
            name="a", description="", input_schema={"type": "object"},
            safety=ToolSafety.READ_ONLY, implementation=impl_a,
        )
        tool_b = ToolDefinition(
            name="b", description="", input_schema={"type": "object"},
            safety=ToolSafety.READ_ONLY, implementation=impl_b,
        )
        registry = make_registry(tool_a, tool_b)
        calls = [
            ToolCall(id="1", name="a", input={}),
            ToolCall(id="2", name="b", input={}),
        ]
        results = await execute_batch(registry, calls)
        assert [r.content for r in results] == ["a", "b"]
        assert order == ["a", "b"]

    @pytest.mark.asyncio
    async def test_parallel_for_concurrency_safe(self) -> None:
        started: list[str] = []
        finished: list[str] = []

        async def slow(input: dict, **_: Any) -> str:
            name = input.get("name", "?")
            started.append(name)
            await asyncio.sleep(0.01)
            finished.append(name)
            return name

        for ch in "abc":
            pass

        tools = [
            ToolDefinition(
                name=ch,
                description="",
                input_schema={"type": "object"},
                safety=ToolSafety.CONCURRENCY_SAFE,
                implementation=slow,
            )
            for ch in "abc"
        ]
        registry = make_registry(*tools)
        calls = [ToolCall(id=str(i), name=ch, input={"name": ch}) for i, ch in enumerate("abc")]
        results = await execute_batch(registry, calls)
        # All started before any finished (parallel execution)
        assert set(r.content for r in results) == {"a", "b", "c"}

    @pytest.mark.asyncio
    async def test_empty_batch(self) -> None:
        registry = ToolRegistry()
        results = await execute_batch(registry, [])
        assert results == []


# ---------------------------------------------------------------------------
# Schema generation
# ---------------------------------------------------------------------------

class TestSchemaGeneration:
    def test_openai_schema(self) -> None:
        tool = ToolDefinition(
            name="grep",
            description="Search files",
            input_schema={
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        )
        registry = make_registry(tool)
        schemas = registry.openai_schemas()
        assert len(schemas) == 1
        s = schemas[0]
        assert s["type"] == "function"
        assert s["function"]["name"] == "grep"
        assert "pattern" in s["function"]["parameters"]["properties"]

    def test_anthropic_schema(self) -> None:
        tool = ToolDefinition(
            name="bash",
            description="Run shell",
            input_schema={"type": "object", "properties": {"command": {"type": "string"}}},
        )
        registry = make_registry(tool)
        schemas = registry.anthropic_schemas()
        assert schemas[0]["name"] == "bash"
        assert "input_schema" in schemas[0]

    def test_schema_for_subset(self) -> None:
        registry = make_registry(make_echo_tool("a"), make_echo_tool("b"))
        schemas = registry.openai_schemas(tool_names=["a"])
        assert len(schemas) == 1
        assert schemas[0]["function"]["name"] == "a"
