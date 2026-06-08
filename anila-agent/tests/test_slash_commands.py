"""P2-10 slash command framework + 10 個 built-in command tests。

涵蓋:

* framework basics:registry / decorator / dispatch / parse / unknown / error 容錯
* 10 個 built-in:help / cost / budget / trace / tools / find / task / memory /
  graph / clear — 每個都驗 "有注入" 與 "沒注入" 兩種路徑
* run_demo_loop_async 整合 slash dispatch(P2-14 整合測)
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from anila_agent.cli.builtin_commands import (
    build_default_registry,
    cmd_budget,
    cmd_clear,
    cmd_cost,
    cmd_find,
    cmd_graph,
    cmd_memory,
    cmd_task,
    cmd_tools,
    cmd_trace,
    register_builtin_commands,
)
from anila_agent.cli.demo_agents import echo_agent_stream
from anila_agent.cli.run_demo_loop import (
    _list_to_async_provider,
    run_demo_loop_async,
)
from anila_agent.cli.slash_commands import (
    SlashCommand,
    SlashCommandContext,
    SlashCommandRegistry,
    default_registry,
    dispatch,
    is_slash_command,
    parse_slash_line,
    reset_default_registry,
    slash_command,
)
from anila_agent.core.cost_tracker import CostTracker, PricingRegistry
from anila_agent.core.streaming import StreamChunk
from anila_agent.core.token_budget import BudgetTracker
from anila_agent.tracing import Tracer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _buffer_console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    console = Console(
        file=buf,
        force_terminal=False,
        no_color=True,
        width=160,
    )
    return console, buf


@pytest.fixture()
def fresh_registry() -> SlashCommandRegistry:
    """每個測試一個全新 registry,避免互相污染。"""
    return SlashCommandRegistry()


@pytest.fixture(autouse=True)
def _reset_global_registry() -> None:
    """每個測試前清空 global default registry,避免 decorator 累積。"""
    reset_default_registry()


# ---------------------------------------------------------------------------
# Framework basics — parsing / is_slash_command
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line,expected",
    [
        ("/help", True),
        ("/cost USD", True),
        ("/   ", False),  # only "/" no name
        ("", False),
        ("hello", False),
        ("//comment", False),  # double slash 視為註解
        ("  /help", False),  # 必須恰好 ``/`` 開頭
    ],
)
def test_is_slash_command(line: str, expected: bool) -> None:
    assert is_slash_command(line) is expected


def test_parse_slash_line_basic() -> None:
    name, args = parse_slash_line("/find auth handler")
    assert name == "find"
    assert args == ["auth", "handler"]


def test_parse_slash_line_quoted_args() -> None:
    """shlex 支援 quoted arg。"""
    name, args = parse_slash_line('/task start scrape "load url"')
    assert name == "task"
    assert args == ["start", "scrape", "load url"]


def test_parse_slash_line_unbalanced_quote_falls_back() -> None:
    """引號未閉合 → fallback 為純空白切,不 raise。"""
    name, args = parse_slash_line('/find "unclosed')
    assert name == "find"
    # fallback split → 兩個 token (含未閉合引號的字面)
    assert len(args) == 1


def test_parse_slash_line_rejects_non_slash() -> None:
    with pytest.raises(ValueError, match="not a slash command"):
        parse_slash_line("help")


def test_parse_slash_line_rejects_empty() -> None:
    with pytest.raises(ValueError):
        parse_slash_line("/")


# ---------------------------------------------------------------------------
# Framework basics — registry add / remove / get / names
# ---------------------------------------------------------------------------


def test_registry_add_and_get(fresh_registry: SlashCommandRegistry) -> None:
    cmd = SlashCommand("ping", "echo pong", lambda a, c: "pong")
    fresh_registry.add(cmd)
    assert "ping" in fresh_registry
    assert fresh_registry.get("ping") is cmd
    assert fresh_registry.get("nope") is None
    assert len(fresh_registry) == 1


def test_registry_add_rejects_duplicate(
    fresh_registry: SlashCommandRegistry,
) -> None:
    fresh_registry.add(SlashCommand("ping", "", lambda a, c: ""))
    with pytest.raises(ValueError, match="already registered"):
        fresh_registry.add(SlashCommand("ping", "", lambda a, c: ""))


def test_registry_add_overwrite(fresh_registry: SlashCommandRegistry) -> None:
    fresh_registry.add(SlashCommand("ping", "v1", lambda a, c: "old"))
    fresh_registry.add(
        SlashCommand("ping", "v2", lambda a, c: "new"),
        overwrite=True,
    )
    assert fresh_registry.get("ping").description == "v2"


def test_registry_remove_is_noop_when_missing(
    fresh_registry: SlashCommandRegistry,
) -> None:
    fresh_registry.remove("does_not_exist")  # 不該 raise


def test_registry_names_sorted(fresh_registry: SlashCommandRegistry) -> None:
    fresh_registry.add(SlashCommand("zoo", "", lambda a, c: ""))
    fresh_registry.add(SlashCommand("apple", "", lambda a, c: ""))
    assert fresh_registry.names() == ["apple", "zoo"]


def test_slash_command_name_must_be_identifier() -> None:
    with pytest.raises(ValueError, match="identifier"):
        SlashCommand("not a name!", "", lambda a, c: "")


# ---------------------------------------------------------------------------
# Framework basics — decorator
# ---------------------------------------------------------------------------


def test_slash_command_decorator_registers_into_default() -> None:
    @slash_command("ping", "pong")
    def _cb(args: list[str], ctx: SlashCommandContext) -> str:
        return "pong"

    assert "ping" in default_registry()


def test_slash_command_decorator_with_explicit_registry(
    fresh_registry: SlashCommandRegistry,
) -> None:
    @slash_command("foo", "", registry=fresh_registry)
    def _cb(args: list[str], ctx: SlashCommandContext) -> str:
        return "foo!"

    assert "foo" in fresh_registry
    # 不該污染 default
    assert "foo" not in default_registry()


# ---------------------------------------------------------------------------
# Framework basics — dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_non_slash_returns_none(
    fresh_registry: SlashCommandRegistry,
) -> None:
    ctx = SlashCommandContext()
    assert await dispatch("hello world", ctx, registry=fresh_registry) is None


@pytest.mark.asyncio
async def test_dispatch_unknown_command(
    fresh_registry: SlashCommandRegistry,
) -> None:
    ctx = SlashCommandContext()
    out = await dispatch("/nope", ctx, registry=fresh_registry)
    assert out is not None
    assert "unknown command" in out
    assert "nope" in out


@pytest.mark.asyncio
async def test_dispatch_sync_callback(
    fresh_registry: SlashCommandRegistry,
) -> None:
    fresh_registry.add(
        SlashCommand("greet", "", lambda args, ctx: f"hi {args[0]}")
    )
    out = await dispatch("/greet world", SlashCommandContext(), registry=fresh_registry)
    assert out == "hi world"


@pytest.mark.asyncio
async def test_dispatch_async_callback(
    fresh_registry: SlashCommandRegistry,
) -> None:
    async def _cb(args: list[str], ctx: SlashCommandContext) -> str:
        return f"async: {' '.join(args)}"

    fresh_registry.add(SlashCommand("async_greet", "", _cb))
    out = await dispatch(
        "/async_greet a b", SlashCommandContext(), registry=fresh_registry
    )
    assert out == "async: a b"


@pytest.mark.asyncio
async def test_dispatch_callback_exception_doesnt_crash(
    fresh_registry: SlashCommandRegistry,
) -> None:
    def _boom(args: list[str], ctx: SlashCommandContext) -> str:
        raise RuntimeError("boom!")

    fresh_registry.add(SlashCommand("boom", "", _boom))
    out = await dispatch("/boom", SlashCommandContext(), registry=fresh_registry)
    assert out is not None
    assert "error" in out
    assert "RuntimeError" in out
    assert "boom!" in out


@pytest.mark.asyncio
async def test_dispatch_empty_slash_returns_error(
    fresh_registry: SlashCommandRegistry,
) -> None:
    # "/" 一個字 → is_slash_command 回 False → dispatch 回 None
    assert await dispatch("/", SlashCommandContext(), registry=fresh_registry) is None


@pytest.mark.asyncio
async def test_dispatch_uses_default_registry_when_none(
) -> None:
    """不指定 registry 應走 default。"""

    @slash_command("foo", "")
    def _foo(args: list[str], ctx: SlashCommandContext) -> str:
        return "foo!"

    out = await dispatch("/foo", SlashCommandContext())
    assert out == "foo!"


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_help_lists_all_commands() -> None:
    registry = build_default_registry()
    out = await dispatch("/help", SlashCommandContext(), registry=registry)
    assert out is not None
    assert "Available slash commands" in out
    for expected in [
        "/help",
        "/cost",
        "/budget",
        "/trace",
        "/tools",
        "/find",
        "/task",
        "/memory",
        "/graph",
        "/clear",
    ]:
        assert expected in out


def test_help_reflects_isolated_registry() -> None:
    """``/help`` 應只列自家 registry 內 command,不會看到 default。"""

    @slash_command("default_only", "")
    def _default_cb(args: list[str], ctx: SlashCommandContext) -> str:
        return ""

    isolated = build_default_registry()
    help_cmd = isolated.get("help")
    out = help_cmd.callback([], SlashCommandContext())
    assert "default_only" not in out
    assert "/cost" in out


# ---------------------------------------------------------------------------
# /cost
# ---------------------------------------------------------------------------


def test_cost_without_tracker() -> None:
    out = cmd_cost([], SlashCommandContext())
    assert "no cost tracker" in out


def test_cost_with_tracker() -> None:
    tracker = CostTracker(PricingRegistry())
    tracker.record(model="gpt-4o", prompt_tokens=10, completion_tokens=5)
    ctx = SlashCommandContext(cost_tracker=tracker)
    out = cmd_cost([], ctx)
    assert "Total cost" in out
    assert "gpt-4o" in out


def test_cost_with_unsupported_type() -> None:
    out = cmd_cost([], SlashCommandContext(cost_tracker=object()))
    assert "unsupported cost tracker" in out


# ---------------------------------------------------------------------------
# /budget
# ---------------------------------------------------------------------------


def test_budget_without_tracker() -> None:
    out = cmd_budget([], SlashCommandContext())
    assert "no token budget" in out


def test_budget_with_tracker() -> None:
    bt = BudgetTracker(max_tokens=1000)
    bt.record(prompt_tokens=100, completion_tokens=50)
    ctx = SlashCommandContext(budget_tracker=bt)
    out = cmd_budget([], ctx)
    assert "150/1000" in out
    assert "remaining" in out
    assert "15.0%" in out


def test_budget_with_unsupported_type() -> None:
    out = cmd_budget([], SlashCommandContext(budget_tracker=object()))
    assert "unsupported budget tracker" in out


# ---------------------------------------------------------------------------
# /trace
# ---------------------------------------------------------------------------


def test_trace_without_tracer() -> None:
    out = cmd_trace([], SlashCommandContext())
    assert "no tracer" in out


def test_trace_with_empty_tracer() -> None:
    tracer = Tracer()
    ctx = SlashCommandContext(tracer=tracer)
    out = cmd_trace([], ctx)
    assert "no traces" in out


def test_trace_with_recorded_traces() -> None:
    tracer = Tracer()
    with tracer.start_trace("op.one"):
        pass
    with tracer.start_trace("op.two"):
        pass
    ctx = SlashCommandContext(tracer=tracer)
    out = cmd_trace([], ctx)
    assert "op.one" in out or "op.two" in out
    assert "recent traces" in out


def test_trace_with_limit_arg() -> None:
    tracer = Tracer()
    for i in range(5):
        with tracer.start_trace(f"op.{i}"):
            pass
    ctx = SlashCommandContext(tracer=tracer)
    out = cmd_trace(["2"], ctx)
    # 應只列 2 條
    lines = out.split("\n")
    trace_lines = [li for li in lines if li.strip().startswith("-")]
    assert len(trace_lines) == 2


def test_trace_invalid_limit_arg() -> None:
    tracer = Tracer()
    ctx = SlashCommandContext(tracer=tracer)
    out = cmd_trace(["abc"], ctx)
    assert "invalid trace limit" in out


# ---------------------------------------------------------------------------
# /tools
# ---------------------------------------------------------------------------


def test_tools_without_registry() -> None:
    out = cmd_tools([], SlashCommandContext())
    assert "no tool registry" in out


def test_tools_with_empty_registry() -> None:
    from anila_agent.tools.registry import ToolRegistry

    reg = ToolRegistry()
    ctx = SlashCommandContext(tool_registry=reg)
    out = cmd_tools([], ctx)
    assert "active tools (0)" in out
    assert "deferred tools: 0" in out


def test_tools_with_some_tools() -> None:
    from anila_agent.tools.base import FunctionTool
    from anila_agent.tools.registry import ToolRegistry

    reg = ToolRegistry()

    async def _on_invoke(_ctx: Any, _input_json: str) -> str:
        return "ok"

    tool = FunctionTool(
        name="my_tool",
        description="say hello",
        params_json_schema={"type": "object", "properties": {}},
        on_invoke_tool=_on_invoke,
    )
    reg.add(tool)
    ctx = SlashCommandContext(tool_registry=reg)
    out = cmd_tools([], ctx)
    assert "active tools (1)" in out
    assert "my_tool" in out
    assert "say hello" in out


def test_tools_with_unsupported_type() -> None:
    out = cmd_tools([], SlashCommandContext(tool_registry=object()))
    assert "unsupported tool registry" in out


# ---------------------------------------------------------------------------
# /find
# ---------------------------------------------------------------------------


def test_find_without_pattern() -> None:
    out = cmd_find([], SlashCommandContext())
    assert "usage" in out


def test_find_without_index() -> None:
    out = cmd_find(["pattern"], SlashCommandContext())
    assert "no file index" in out


def test_find_with_index(tmp_path: Path) -> None:
    from anila_agent.tools.file_index import FileIndex

    # 建幾個檔案
    (tmp_path / "auth.py").write_text("a", encoding="utf-8")
    (tmp_path / "user_handler.py").write_text("b", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("c", encoding="utf-8")

    idx = FileIndex(tmp_path)
    idx.build()
    ctx = SlashCommandContext(file_index=idx)
    out = cmd_find(["auth"], ctx)
    assert "auth.py" in out
    assert "matches for" in out


def test_find_no_matches(tmp_path: Path) -> None:
    from anila_agent.tools.file_index import FileIndex

    (tmp_path / "a.py").write_text("", encoding="utf-8")
    idx = FileIndex(tmp_path)
    idx.build()
    out = cmd_find(["definitely_not_present_xyz"], SlashCommandContext(file_index=idx))
    assert "no matches" in out


# ---------------------------------------------------------------------------
# /task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_without_manager() -> None:
    out = await cmd_task(["list"], SlashCommandContext())
    assert "no task manager" in out


@pytest.mark.asyncio
async def test_task_list_empty() -> None:
    from anila_agent.core.task_manager import TaskManager

    mgr = TaskManager()
    out = await cmd_task(["list"], SlashCommandContext(task_manager=mgr))
    assert "no background tasks" in out


@pytest.mark.asyncio
async def test_task_start_unknown_type() -> None:
    from anila_agent.core.task_manager import TaskManager
    from anila_agent.tools.task import _clear_task_type_registry

    _clear_task_type_registry()
    mgr = TaskManager()
    out = await cmd_task(
        ["start", "unknown_type", "do something"],
        SlashCommandContext(task_manager=mgr),
    )
    assert "unknown task type" in out


@pytest.mark.asyncio
async def test_task_start_registered_type() -> None:
    from anila_agent.core.task_manager import TaskManager
    from anila_agent.tools.task import (
        _clear_task_type_registry,
        register_task_type,
    )

    _clear_task_type_registry()

    async def _my_task() -> str:
        return "done"

    register_task_type("dummy", _my_task)

    mgr = TaskManager()
    out = await cmd_task(
        ["start", "dummy", "test task"],
        SlashCommandContext(task_manager=mgr),
    )
    assert "started task" in out
    assert "dummy" in out

    # 應該真的有一筆 task 被建立
    tasks = await mgr.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].description == "test task"

    _clear_task_type_registry()


@pytest.mark.asyncio
async def test_task_start_missing_args() -> None:
    from anila_agent.core.task_manager import TaskManager

    mgr = TaskManager()
    out = await cmd_task(["start"], SlashCommandContext(task_manager=mgr))
    assert "usage" in out


@pytest.mark.asyncio
async def test_task_unknown_subcommand() -> None:
    from anila_agent.core.task_manager import TaskManager

    mgr = TaskManager()
    out = await cmd_task(["weird"], SlashCommandContext(task_manager=mgr))
    assert "usage" in out


# ---------------------------------------------------------------------------
# /memory
# ---------------------------------------------------------------------------


def test_memory_without_data() -> None:
    out = cmd_memory([], SlashCommandContext())
    assert "no session memory" in out


def test_memory_with_single_entry() -> None:
    from anila_agent.memory.session_memory import SessionMemory

    mem = SessionMemory(
        session_id="sess-1",
        summary="一段測試 summary",
        topics=("rag", "auth"),
        message_count=42,
    )
    ctx = SlashCommandContext(session_memory=mem)
    out = cmd_memory([], ctx)
    assert "sess-1" in out
    assert "42 msgs" in out
    assert "rag" in out


def test_memory_with_list() -> None:
    from anila_agent.memory.session_memory import SessionMemory

    mems = [
        SessionMemory(session_id="s1", summary="a", topics=("x",)),
        SessionMemory(session_id="s2", summary="b", topics=("y",)),
    ]
    out = cmd_memory([], SlashCommandContext(session_memory=mems))
    assert "2 entries" in out
    assert "s1" in out
    assert "s2" in out


def test_memory_with_empty_list() -> None:
    out = cmd_memory([], SlashCommandContext(session_memory=[]))
    assert "no session memory entries" in out


# ---------------------------------------------------------------------------
# /graph
# ---------------------------------------------------------------------------


def test_graph_without_agent() -> None:
    out = cmd_graph([], SlashCommandContext())
    assert "no agent" in out


def test_graph_with_stub_agent() -> None:
    from anila_agent.cli.draw_graph_cli import build_stub_agent

    agent = build_stub_agent()
    ctx = SlashCommandContext(agent=agent)
    out = cmd_graph([], ctx)
    # ASCII tree 第一行為 [<agent.name>]
    assert "[" in out
    assert agent.name in out


# ---------------------------------------------------------------------------
# /clear
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_without_clearer() -> None:
    out = await cmd_clear([], SlashCommandContext())
    assert "no history clearer" in out


@pytest.mark.asyncio
async def test_clear_calls_sync_clearer() -> None:
    called = {"n": 0}

    def _clearer() -> None:
        called["n"] += 1

    out = await cmd_clear([], SlashCommandContext(history_clearer=_clearer))
    assert called["n"] == 1
    assert "cleared" in out


@pytest.mark.asyncio
async def test_clear_calls_async_clearer() -> None:
    called = {"n": 0}

    async def _clearer() -> None:
        called["n"] += 1

    out = await cmd_clear([], SlashCommandContext(history_clearer=_clearer))
    assert called["n"] == 1
    assert "cleared" in out


@pytest.mark.asyncio
async def test_clear_handles_clearer_exception() -> None:
    def _broken() -> None:
        raise RuntimeError("nope")

    out = await cmd_clear([], SlashCommandContext(history_clearer=_broken))
    assert "failed to clear" in out
    assert "RuntimeError" in out


# ---------------------------------------------------------------------------
# register_builtin_commands / build_default_registry
# ---------------------------------------------------------------------------


def test_register_builtin_commands_into_explicit_registry() -> None:
    reg = SlashCommandRegistry()
    register_builtin_commands(reg)
    assert len(reg) == 10
    expected = {
        "help",
        "cost",
        "budget",
        "trace",
        "tools",
        "find",
        "task",
        "memory",
        "graph",
        "clear",
    }
    assert set(reg.names()) == expected


def test_register_builtin_commands_default() -> None:
    """不指定 registry 應註冊到 default。"""
    reset_default_registry()
    register_builtin_commands()
    assert len(default_registry()) == 10


def test_register_builtin_overwrite_false_rejects_duplicate() -> None:
    reg = SlashCommandRegistry()
    register_builtin_commands(reg)
    with pytest.raises(ValueError):
        register_builtin_commands(reg)


def test_register_builtin_overwrite_true_allowed() -> None:
    reg = SlashCommandRegistry()
    register_builtin_commands(reg)
    register_builtin_commands(reg, overwrite=True)
    assert len(reg) == 10


def test_build_default_registry_independent_from_global() -> None:
    """:func:`build_default_registry` 不該污染 default。"""
    reset_default_registry()
    isolated = build_default_registry()
    assert len(isolated) == 10
    assert len(default_registry()) == 0


# ---------------------------------------------------------------------------
# run_demo_loop_async 整合
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_demo_loop_async_dispatches_slash_help() -> None:
    """slash_registry 注入後,/help 應被 dispatch、不丟給 agent。"""
    registry = build_default_registry()
    console, buf = _buffer_console()
    provider = _list_to_async_provider(["/help", "quit"])

    result = await run_demo_loop_async(
        echo_agent_stream,
        input_provider=provider,
        console=console,
        slash_registry=registry,
    )
    # /help 不算「agent turn」— 不該 +1
    assert result["turns"] == 0
    text = buf.getvalue()
    assert "Available slash commands" in text


@pytest.mark.asyncio
async def test_run_demo_loop_async_slash_unknown_doesnt_crash() -> None:
    registry = build_default_registry()
    console, buf = _buffer_console()
    provider = _list_to_async_provider(["/nope", "quit"])

    result = await run_demo_loop_async(
        echo_agent_stream,
        input_provider=provider,
        console=console,
        slash_registry=registry,
    )
    assert result["turns"] == 0
    assert "unknown command" in buf.getvalue()


@pytest.mark.asyncio
async def test_run_demo_loop_async_no_registry_treats_slash_as_input() -> None:
    """沒注入 registry 時,/foo 應原樣丟給 agent(echo 會吐 'echo: /foo')。"""
    console, buf = _buffer_console()
    provider = _list_to_async_provider(["/foo", "quit"])

    result = await run_demo_loop_async(
        echo_agent_stream,
        input_provider=provider,
        console=console,
    )
    assert result["turns"] == 1
    assert "echo: /foo" in buf.getvalue()


@pytest.mark.asyncio
async def test_run_demo_loop_async_slash_cost_uses_injected_tracker() -> None:
    """/cost 應印 injected CostTracker 的內容。"""
    registry = build_default_registry()
    tracker = CostTracker(PricingRegistry())
    tracker.record(model="gpt-4o", prompt_tokens=100, completion_tokens=50)

    console, buf = _buffer_console()
    # 走預設 ctx → effective_slash_ctx 會用 outer ``tracker``
    provider = _list_to_async_provider(["/cost", "quit"])

    await run_demo_loop_async(
        echo_agent_stream,
        input_provider=provider,
        console=console,
        slash_registry=registry,
        cost_tracker=tracker,
    )
    text = buf.getvalue()
    assert "gpt-4o" in text


@pytest.mark.asyncio
async def test_run_demo_loop_async_mixed_slash_and_agent_turns() -> None:
    """slash command 與 agent turn 應混合運作。"""
    registry = build_default_registry()
    console, _ = _buffer_console()
    provider = _list_to_async_provider(["/help", "hello", "/help", "quit"])

    result = await run_demo_loop_async(
        echo_agent_stream,
        input_provider=provider,
        console=console,
        slash_registry=registry,
    )
    # 兩個 /help 不算 turn,只有 "hello" 算 1 turn
    assert result["turns"] == 1


@pytest.mark.asyncio
async def test_run_demo_loop_async_custom_slash_context() -> None:
    """caller 可注入自家 SlashCommandContext。"""
    registry = SlashCommandRegistry()

    def _whoami(args: list[str], ctx: SlashCommandContext) -> str:
        return f"hello {ctx.extras.get('user', 'anon')}"

    registry.add(SlashCommand("whoami", "show user", _whoami))

    ctx = SlashCommandContext(extras={"user": "alice"})
    console, buf = _buffer_console()
    provider = _list_to_async_provider(["/whoami", "quit"])

    await run_demo_loop_async(
        echo_agent_stream,
        input_provider=provider,
        console=console,
        slash_registry=registry,
        slash_context=ctx,
    )
    assert "hello alice" in buf.getvalue()


# ---------------------------------------------------------------------------
# 邊角測試:確保不破壞既有 streaming demo 行為
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_demo_loop_async_double_slash_treated_as_input() -> None:
    """``//`` 開頭視為一般 user input — 不 dispatch。"""
    registry = build_default_registry()
    console, _buf = _buffer_console()
    provider = _list_to_async_provider(["//note this is a comment", "quit"])

    result = await run_demo_loop_async(
        echo_agent_stream,
        input_provider=provider,
        console=console,
        slash_registry=registry,
    )
    assert result["turns"] == 1


@pytest.mark.asyncio
async def test_run_demo_loop_async_slash_with_broken_callback_continues() -> None:
    """slash callback 拋例外時不該 crash 整個 loop。"""
    registry = SlashCommandRegistry()

    def _boom(args: list[str], ctx: SlashCommandContext) -> str:
        raise RuntimeError("nope")

    registry.add(SlashCommand("boom", "", _boom))

    console, buf = _buffer_console()
    provider = _list_to_async_provider(["/boom", "quit"])

    result = await run_demo_loop_async(
        echo_agent_stream,
        input_provider=provider,
        console=console,
        slash_registry=registry,
    )
    assert result["turns"] == 0
    text = buf.getvalue()
    assert "RuntimeError" in text


# ---------------------------------------------------------------------------
# StreamChunk 型別實驗 — 不直接 testable,但讓 type checker 滿意
# ---------------------------------------------------------------------------


async def _smoke_chunks(_inp: str) -> AsyncIterator[StreamChunk]:
    """unused — 只是為了 type 檢查 import 一直在用。"""
    yield {"kind": "agent_started", "agent": "x"}
