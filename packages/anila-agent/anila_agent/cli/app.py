"""互動式 REPL —— 串流渲染 + slash 指令。

短期記憶走 SDK 原生 Session（main 傳入；預設 SQLite）。slash 指令：
  /help            列出指令
  /memory [查詢]   列出或檢索長期記憶（需 ANILA_MEMORY=1）
  /style           列出可用 output style
  /clear           清空本對話歷史
  /<檔案指令> ...   configs/commands/<name>.md 的 macro
輸入 exit / quit 離開。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from agents import TResponseInputItem
from agents.result import RunResultStreaming
from agents.stream_events import (
    AgentUpdatedStreamEvent,
    RawResponsesStreamEvent,
    RunItemStreamEvent,
)
from openai.types.responses.response_text_delta_event import ResponseTextDeltaEvent
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console

from anila_agent.cli.output_styles import list_output_styles
from anila_agent.cli.slash_commands import SlashCommand, load_commands, parse_slash
from anila_agent.config import AppConfig
from anila_agent.memory.runtime import auto_memory_enabled
from anila_agent.runtime.agent_factory import AssembledAgent
from anila_agent.runtime.run import run_streamed

if TYPE_CHECKING:
    from agents import Session

_EXIT = {"exit", "quit", ":q"}
_console = Console()


def _banner(assembled: AssembledAgent) -> None:
    backend = assembled.context.retriever.name
    model = getattr(assembled.agent.model, "model", "?")
    memdir = "on" if assembled.context.memory is not None else "off"
    _console.print(
        f"[bold cyan]ANILA Agent[/]  model=[green]{model}[/]  "
        f"retriever=[green]{backend}[/]  memdir=[green]{memdir}[/]"
    )
    _console.print("[dim]輸入問題開始；/help 看指令；exit / quit 離開。[/]")


async def _handle_slash(
    name: str,
    args: str,
    assembled: AssembledAgent,
    commands: dict[str, SlashCommand],
    chat_session: Session | None,
    cfg: AppConfig | None = None,
) -> str | None:
    """處理 slash 指令。回傳要送給 agent 的字串，或 None（已就地處理）。"""
    if name == "help":
        _console.print("[bold]內建指令[/]：/help /memory [查詢] /style /clear /deep-research <問題>")
        if commands:
            _console.print("[bold]檔案指令[/]：" + " ".join(f"/{n}" for n in commands))
        return None
    if name in ("deep-research", "deep_research"):
        if cfg is None or not args.strip():
            _console.print("[yellow]用法：/deep-research <問題>[/]")
            return None
        from anila_agent.orchestration.deep_research import run_deep_research

        _console.print("[dim][深度檢索中：規劃 → 平行檢索 → 綜合…][/]")
        answer = await run_deep_research(cfg, args.strip(), assembled.context.retriever)
        _console.print(answer)
        return None
    if name == "clear":
        if chat_session is not None:
            await chat_session.clear_session()
        _console.print("[dim]已清空本對話歷史。[/]")
        return None
    if name == "style":
        _console.print("可用 output style：" + ", ".join(list_output_styles()))
        _console.print("[dim]以 ANILA_OUTPUT_STYLE=<name> 啟用。[/]")
        return None
    if name == "memory":
        mem = assembled.context.memory
        if mem is None:
            _console.print("[yellow]memdir 未啟用（設 ANILA_MEMORY=1）。[/]")
            return None
        if args.strip():
            hits = await mem.recall_bodies(args.strip(), k=5)
            for m in hits:
                _console.print(f"[green]{m.name}[/] ({m.type}): {m.description if hasattr(m,'description') else ''}")
                _console.print(f"  {m.body[:200]}")
            if not hits:
                _console.print("[dim]查無相關記憶。[/]")
        else:
            _console.print(mem.store.index_text())
        return None
    if name in commands:
        return commands[name].expand(args)
    _console.print(f"[yellow]未知指令 /{name}（/help 看清單）。[/]")
    return None


async def _stream(result: RunResultStreaming) -> str:
    """渲染串流並回傳累積的助理答案（供 turn 結束後自動抽取記憶）。"""
    parts: list[str] = []
    async for event in result.stream_events():
        if isinstance(event, RawResponsesStreamEvent):
            if isinstance(event.data, ResponseTextDeltaEvent):
                parts.append(event.data.delta)
                print(event.data.delta, end="", flush=True)
        elif isinstance(event, RunItemStreamEvent):
            if event.item.type == "tool_call_item":
                _console.print("[dim]\n[檢索中…][/]")
            elif event.item.type == "tool_call_output_item":
                _console.print("[dim][檢索完成][/]")
        elif isinstance(event, AgentUpdatedStreamEvent):
            _console.print(f"[dim]\n[切換 agent：{event.new_agent.name}][/]")
    print()
    return "".join(parts)


async def repl(
    assembled: AssembledAgent,
    chat_session: Session | None = None,
    cfg: AppConfig | None = None,
) -> None:
    """跑互動迴圈。``chat_session`` 提供時由 SDK 管理跨輪歷史；``cfg`` 供 /deep-research 用。"""
    _banner(assembled)
    prompt: PromptSession[str] = PromptSession()
    commands = load_commands()
    fallback_items: list[TResponseInputItem] = []

    while True:
        try:
            with patch_stdout():
                user_input = await prompt.prompt_async("\n> ")
        except (EOFError, KeyboardInterrupt):
            _console.print()
            break

        text = user_input.strip()
        if not text:
            continue
        if text.lower() in _EXIT:
            break

        slash = parse_slash(text)
        if slash is not None:
            to_send = await _handle_slash(slash[0], slash[1], assembled, commands, chat_session, cfg)
            if not to_send:  # None 或展開後為空 → 不送 agent
                continue
            user_input = to_send

        if chat_session is not None:
            result = run_streamed(assembled, user_input, session=chat_session)
        else:
            fallback_items.append(
                cast(TResponseInputItem, {"role": "user", "content": user_input})
            )
            result = run_streamed(assembled, fallback_items)

        answer = await _stream(result)

        if chat_session is None:
            fallback_items = result.to_input_list()

        # 自動抽取長期記憶（ANILA_AUTO_MEMORY=1）：去敏後寫入 store。
        mem = assembled.context.memory
        if mem is not None and auto_memory_enabled() and answer.strip():
            try:
                written = await mem.absorb_turn(user_input, answer)
                if written:
                    _console.print(f"[dim][已記住：{', '.join(written)}][/]")
            except Exception:
                _console.print("[dim][記憶抽取失敗，已略過][/]")
