"""Slash-command parser. Ported in shape from claude-code-src `commands.ts`.

Each command is one of:
  - 'local'  — a sync/async Python callback. The CLI does not call the model.
  - 'prompt' — returns text that is fed to the agent as the next user message.

This module is host-agnostic — it does not import the CLI app. The CLI imports
`dispatch()` and decides what to do with the returned payload.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Union

CommandKind = Literal["local", "prompt"]
LocalCallback = Callable[[list[str], "CommandContext"], Union[str, Awaitable[str]]]
PromptCallback = Callable[[list[str], "CommandContext"], Union[str, Awaitable[str]]]


@dataclass
class CommandContext:
    """Mutable state passed to commands. Add fields here as the CLI grows."""

    runner: Any  # AnilaRunner — typed loose to avoid an import cycle
    config: Any  # AppConfig
    extras: dict[str, Any]


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    kind: CommandKind
    callback: LocalCallback | PromptCallback


@dataclass(frozen=True)
class DispatchResult:
    """Returned from `dispatch()`."""

    handled: bool
    kind: CommandKind | None
    output: str
    exit: bool = False


def is_command(line: str) -> bool:
    return line.startswith("/")


def parse(line: str) -> tuple[str, list[str]]:
    """Split a slash line into (name, args). Quoted args are not supported — keep it simple."""
    if not line.startswith("/"):
        raise ValueError("not a slash command")
    parts = line[1:].split()
    if not parts:
        raise ValueError("empty slash command")
    return parts[0], parts[1:]


# ---------------------------------------------------------------------------
# Built-in commands
# ---------------------------------------------------------------------------


async def _help(_args: list[str], ctx: CommandContext) -> str:
    lines = ["Available commands:"]
    for cmd in ctx.extras["registry"].values():
        lines.append(f"  /{cmd.name:<10} {cmd.description}")
    return "\n".join(lines)


async def _exit(_args: list[str], _ctx: CommandContext) -> str:
    return "bye"


async def _clear(_args: list[str], ctx: CommandContext) -> str:
    session = ctx.runner.assembled.short_term
    if session is None:
        return "no session active"
    await session.clear_session()
    return "session cleared"


async def _model(args: list[str], ctx: CommandContext) -> str:
    cfg = ctx.config.model
    if not args:
        return f"model={cfg.model}\nbase_url={cfg.base_url}"
    return "model is read-only at runtime; edit configs/model.yaml or set ANILA_MODEL"


async def _memory(args: list[str], ctx: CommandContext) -> str:
    long_term = ctx.runner.assembled.long_term
    if long_term is None:
        return "long-term memory disabled"
    sub = args[0] if args else "list"
    if sub == "list":
        index = long_term.list_index().strip()
        return index or "MEMORY.md is empty"
    if sub == "scan":
        headers = long_term.store.scan()
        return long_term.store.format_manifest(headers) or "no memory files"
    if sub == "extract":
        from anila_agent.memory.summarizer import extract_now, is_enabled

        if not is_enabled():
            return "auto memory is disabled in configs/memory.yaml"
        last = ctx.extras.get("last_turn_text", "")
        saved = extract_now(last)
        return f"saved: {', '.join(saved)}" if saved else "nothing extracted"
    return "usage: /memory [list|scan|extract]"


async def _cost(_args: list[str], ctx: CommandContext) -> str:
    bus_metrics: dict[str, int] = ctx.extras.setdefault("metrics", {})
    return (
        f"turns: {bus_metrics.get('turns', 0)}, "
        f"tools: {bus_metrics.get('tools', 0)}, "
        f"errors: {bus_metrics.get('errors', 0)}"
    )


def builtin_registry() -> dict[str, Command]:
    return {
        cmd.name: cmd
        for cmd in [
            Command("help", "Show this help", "local", _help),
            Command("exit", "Quit the REPL", "local", _exit),
            Command("clear", "Clear short-term session history", "local", _clear),
            Command("model", "Show or edit the active model", "local", _model),
            Command("memory", "List/scan/extract long-term memory", "local", _memory),
            Command("cost", "Show session metrics", "local", _cost),
        ]
    }


async def dispatch(line: str, ctx: CommandContext) -> DispatchResult:
    """Resolve a slash line. Non-slash lines return `handled=False` unchanged."""
    import inspect

    if not is_command(line):
        return DispatchResult(handled=False, kind=None, output=line)

    try:
        name, args = parse(line)
    except ValueError as e:
        return DispatchResult(handled=True, kind="local", output=f"error: {e}")

    registry: dict[str, Command] = ctx.extras["registry"]
    cmd = registry.get(name)
    if cmd is None:
        return DispatchResult(handled=True, kind="local", output=f"unknown command: /{name}")

    result = cmd.callback(args, ctx)
    if inspect.isawaitable(result):
        result = await result
    text = str(result)
    return DispatchResult(
        handled=True,
        kind=cmd.kind,
        output=text,
        exit=(name == "exit"),
    )
