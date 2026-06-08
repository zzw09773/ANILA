"""anila_agent.cli — REPL、command dispatch、rich-based renderer。"""

from __future__ import annotations

from anila_agent.cli.approval_repl import (
    InputProvider,
    ReplResult,
    run_approval_repl,
)
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
from anila_agent.cli.demo_agents import (
    DemoAgent,
    echo_agent_stream,
    list_demo_agents,
    weather_agent_stream,
)
from anila_agent.cli.draw_graph_cli import (
    build_stub_agent,
)
from anila_agent.cli.draw_graph_cli import (
    main as draw_graph_cli_main,
)
from anila_agent.cli.run_demo_loop import (
    DEFAULT_STOP_WORDS,
    AsyncInputProvider,
    run_demo_loop,
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

__all__ = [
    # P1-8 approval REPL
    "InputProvider",
    "ReplResult",
    "run_approval_repl",
    # P2-13 draw_graph CLI
    "build_stub_agent",
    "draw_graph_cli_main",
    # P2-14 run_demo_loop
    "DEFAULT_STOP_WORDS",
    "AsyncInputProvider",
    "DemoAgent",
    "echo_agent_stream",
    "list_demo_agents",
    "run_demo_loop",
    "run_demo_loop_async",
    "weather_agent_stream",
    # P2-10 slash command framework
    "SlashCommand",
    "SlashCommandContext",
    "SlashCommandRegistry",
    "default_registry",
    "dispatch",
    "is_slash_command",
    "parse_slash_line",
    "reset_default_registry",
    "slash_command",
    # P2-10 built-in commands
    "build_default_registry",
    "cmd_budget",
    "cmd_clear",
    "cmd_cost",
    "cmd_find",
    "cmd_graph",
    "cmd_memory",
    "cmd_task",
    "cmd_tools",
    "cmd_trace",
    "register_builtin_commands",
]
