"""anila_agent.cli — REPL、command dispatch、rich-based renderer。"""

from __future__ import annotations

from anila_agent.cli.approval_repl import (
    InputProvider,
    ReplResult,
    run_approval_repl,
)
from anila_agent.cli.draw_graph_cli import (
    build_stub_agent,
)
from anila_agent.cli.draw_graph_cli import (
    main as draw_graph_cli_main,
)

__all__ = [
    "InputProvider",
    "ReplResult",
    "build_stub_agent",
    "draw_graph_cli_main",
    "run_approval_repl",
]
