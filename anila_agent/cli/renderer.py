"""Terminal output rendering. `rich`-backed, but the surface is small enough to swap."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from anila_agent.core.events import Event, EventBus

_console = Console()


def console() -> Console:
    return _console


def banner(title: str, subtitle: str | None = None) -> None:
    body = Text(title, style="bold cyan")
    if subtitle:
        body.append(f"\n{subtitle}", style="dim")
    _console.print(Panel(body, border_style="cyan"))


def user_line(text: str) -> None:
    _console.print(Text(f"> {text}", style="bold"))


def assistant_text(text: str) -> None:
    if not text:
        return
    try:
        _console.print(Markdown(text))
    except Exception:  # noqa: BLE001
        _console.print(text)


def system(text: str, *, style: str = "yellow") -> None:
    _console.print(Text(text, style=style))


def error(text: str) -> None:
    _console.print(Text(text, style="bold red"))


def install_event_listeners(bus: EventBus, *, verbose: bool = False) -> None:
    """Wire common events to terminal output. Caller can register more on top."""

    def on_tool_started(ev: Event) -> None:
        tool = ev.payload.get("tool", "?")
        if verbose:
            args = ev.payload.get("input", {})
            _console.print(Text(f"  → {tool}({args})", style="dim cyan"))
        else:
            _console.print(Text(f"  → {tool}", style="dim cyan"))

    def on_tool_ended(ev: Event) -> None:
        if verbose:
            tool = ev.payload.get("tool", "?")
            _console.print(Text(f"  ← {tool}", style="dim cyan"))

    def on_error(ev: Event) -> None:
        error(f"runtime error: {ev.payload.get('message', '?')}")

    bus.on("tool_started", on_tool_started)
    bus.on("tool_ended", on_tool_ended)
    bus.on("error", on_error)


def render_summary(summary: Any) -> None:
    if getattr(summary, "aborted", False):
        error(f"aborted: {summary.abort_reason}")
        return
    output = getattr(summary, "final_output", None)
    if output is None:
        system("(no output)", style="dim")
        return
    text = output if isinstance(output, str) else str(output)
    assistant_text(text)
