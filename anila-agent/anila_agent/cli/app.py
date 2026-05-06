"""REPL loop. `prompt_toolkit` for input, `rich` for output."""

from __future__ import annotations

import asyncio
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout

from anila_agent.cli import commands, renderer
from anila_agent.core.events import Event
from anila_agent.core.runner import AnilaRunner
from anila_agent.utils.config import AppConfig, anila_home


def _metrics_listener(metrics: dict[str, int]) -> Any:
    def listener(event: Event) -> None:
        if event.kind == "turn_ended":
            metrics["turns"] = metrics.get("turns", 0) + 1
        elif event.kind == "tool_started":
            metrics["tools"] = metrics.get("tools", 0) + 1
        elif event.kind == "error":
            metrics["errors"] = metrics.get("errors", 0) + 1

    return listener


async def run(runner: AnilaRunner, config: AppConfig) -> None:
    home = anila_home()
    home.mkdir(parents=True, exist_ok=True)
    session = PromptSession(history=FileHistory(str(home / "history.txt")))

    metrics: dict[str, int] = {}
    runner.assembled.event_bus.on_any(_metrics_listener(metrics))

    last_turn_text: dict[str, str] = {"text": ""}
    ctx = commands.CommandContext(
        runner=runner,
        config=config,
        extras={
            "registry": commands.builtin_registry(),
            "metrics": metrics,
            "last_turn_text": "",
        },
    )

    renderer.install_event_listeners(runner.assembled.event_bus, verbose=False)
    renderer.banner(
        f"anila-agent · {config.agent.name}",
        f"model={config.model.model}  base_url={config.model.base_url or '(default)'}",
    )
    renderer.system("Type /help for commands, /exit to quit.", style="dim")

    await runner.start()

    while True:
        try:
            with patch_stdout():
                line = await session.prompt_async("> ")
        except (EOFError, KeyboardInterrupt):
            renderer.system("\nbye")
            return

        line = line.strip()
        if not line:
            continue

        if commands.is_command(line):
            ctx.extras["last_turn_text"] = last_turn_text["text"]
            result = await commands.dispatch(line, ctx)
            if result.kind == "local":
                renderer.system(result.output)
                if result.exit:
                    return
                continue
            if result.kind == "prompt":
                line = result.output

        renderer.user_line(line)
        try:
            summary = await runner.send(line)
        except Exception as e:  # noqa: BLE001
            renderer.error(f"runtime error: {e}")
            continue

        renderer.render_summary(summary)
        if summary.final_output:
            last_turn_text["text"] = (
                summary.final_output
                if isinstance(summary.final_output, str)
                else str(summary.final_output)
            )


def main_sync(runner: AnilaRunner, config: AppConfig) -> None:
    asyncio.run(run(runner, config))
