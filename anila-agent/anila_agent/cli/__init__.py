"""anila_agent.cli — REPL、command dispatch、rich-based renderer。"""

from __future__ import annotations

from anila_agent.cli.approval_repl import (
    InputProvider,
    ReplResult,
    run_approval_repl,
)
from anila_agent.cli.demo_agents import (
    DemoAgent,
    echo_agent_stream,
    list_demo_agents,
    weather_agent_stream,
)
from anila_agent.cli.run_demo_loop import (
    DEFAULT_STOP_WORDS,
    AsyncInputProvider,
    run_demo_loop,
    run_demo_loop_async,
)

__all__ = [
    "DEFAULT_STOP_WORDS",
    "AsyncInputProvider",
    "DemoAgent",
    "InputProvider",
    "ReplResult",
    "echo_agent_stream",
    "list_demo_agents",
    "run_approval_repl",
    "run_demo_loop",
    "run_demo_loop_async",
    "weather_agent_stream",
]
