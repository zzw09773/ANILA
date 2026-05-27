"""P1-8 Approval REPL — CLI 介面讓 human 對 paused RunState 做 approve / deny。

本模組是 :mod:`anila_agent.core.run_state` 的 CLI 前端,在 CLI 場景下接收
從 :class:`JsonFileStateStore` 載入的 paused state,以 ``rich`` 印出
``pending_tool_calls`` 與 metadata,並用 ``input()`` 接收 approve / deny /
continue / quit 指令。

# 為什麼 REPL 寫成可注入 input

* **單元測試**:用 ``monkeypatch`` 把 :func:`builtins.input` 換成測試 stub,
  即可驗證一連串指令會把 state 改成什麼樣子,不需要 spawn subprocess。
* **嵌入**:caller 可以把 input source 換成 prompt_toolkit 的 ``PromptSession``
  或 Web UI WebSocket 訊息,核心 dispatch logic 不必改。

# 指令文法

* ``approve <call_id>`` — approve 指定 tool call。
* ``deny <call_id> <reason...>`` — deny 指定 tool call,reason 為多字串組合。
* ``continue`` — 嘗試 resume(state 必須沒有 pending tool call)。
* ``quit`` / ``exit`` — 不做任何修改、結束 REPL。
* ``help`` — 印指令清單。
* ``status`` — 重印當前 state 摘要。

每次 approve / deny / continue 成功後,REPL 會用 :class:`JsonFileStateStore`
把 state 寫回硬碟,確保 crash recovery 可行。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TextIO

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from anila_agent.core.run_state import (
    HITLController,
    JsonFileStateStore,
    RunState,
)

logger = logging.getLogger(__name__)


# Input provider:把「拿一行 user input」抽成 callable,REPL 不直接 import builtins.input,
# 方便測試用 monkeypatch 換成 stub。預設用 :func:`input`。
InputProvider = Callable[[str], str]


# REPL 結束後 caller 想知道「state 有沒有變動」/「user 是否要求結束」,用 dataclass 傳。
@dataclass(frozen=True)
class ReplResult:
    """REPL 跑完一輪的結果摘要。

    Attributes:
        state: 結束時的 :class:`RunState`(同 caller 傳入的物件,可能已被就地修改)。
        modified: 是否有任何 approve / deny / continue 動作成功執行。
        quit_requested: user 是否用 ``quit`` / ``exit`` 結束。
        resumed: user 是否成功 ``continue`` 把 state 轉回 running。
    """

    state: RunState
    modified: bool
    quit_requested: bool
    resumed: bool


def _print_state(state: RunState, console: Console) -> None:
    """以 rich 印出 state 概要 + pending tool call table。"""
    header = Text()
    header.append("run_id: ", style="dim")
    header.append(f"{state.run_id}\n", style="bold cyan")
    header.append("agent: ", style="dim")
    header.append(f"{state.agent_name}\n", style="bold")
    header.append("status: ", style="dim")
    header.append(
        f"{state.status}",
        style="yellow" if state.is_paused else "green",
    )
    if state.pause_reason is not None:
        header.append("  reason: ", style="dim")
        header.append(state.pause_reason.value, style="magenta")
    console.print(Panel(header, title="RunState", border_style="cyan"))

    if not state.pending_tool_calls:
        console.print(Text("(no pending tool calls)", style="dim"))
        return

    table = Table(
        title="Pending tool calls",
        show_header=True,
        header_style="bold",
        border_style="dim",
    )
    table.add_column("call_id", style="cyan", no_wrap=True)
    table.add_column("tool", style="bold")
    table.add_column("args")
    for tc in state.pending_tool_calls:
        table.add_row(tc.call_id, tc.name, repr(tc.args))
    console.print(table)


def _print_help(console: Console) -> None:
    """印出指令清單。"""
    body = Text()
    body.append("approve <call_id>            ", style="green")
    body.append("approve a pending tool call\n", style="dim")
    body.append("deny <call_id> <reason...>   ", style="red")
    body.append("deny a pending tool call with a reason\n", style="dim")
    body.append("continue                     ", style="yellow")
    body.append("resume the run (requires no pending calls)\n", style="dim")
    body.append("status                       ", style="cyan")
    body.append("re-print the current state summary\n", style="dim")
    body.append("help                         ", style="cyan")
    body.append("show this help\n", style="dim")
    body.append("quit / exit                  ", style="cyan")
    body.append("leave the REPL without changes", style="dim")
    console.print(Panel(body, title="Commands", border_style="dim"))


def _parse_command(line: str) -> tuple[str, list[str]]:
    """把一行 user input 拆成 (verb, args)。

    空字串會回 ``("", [])``;caller 用 verb == "" 跳過。
    """
    parts = line.strip().split()
    if not parts:
        return "", []
    return parts[0].lower(), parts[1:]


def run_approval_repl(
    state: RunState,
    store: JsonFileStateStore,
    *,
    controller: HITLController | None = None,
    input_provider: InputProvider | None = None,
    console: Console | None = None,
    file: TextIO | None = None,
    max_iterations: int = 1000,
) -> ReplResult:
    """跑一輪 approval REPL,直到 user ``continue`` / ``quit`` 為止。

    Args:
        state: paused 狀態的 :class:`RunState`(由 caller 從 store 載入後傳入)。
        store: :class:`JsonFileStateStore`,REPL 在每次修改後 ``save(state)``。
        controller: :class:`HITLController`;預設新建一個(stateless)。
        input_provider: 接收一行 user input 的 callable;預設用 ``input``。
            單元測試把這裡換成 stub 即可控制流程。
        console: ``rich.Console``;預設新建一個。若想把輸出收進 buffer 給測試
            驗,可自己建 ``Console(file=io.StringIO(), force_terminal=False)``。
        file: 若 ``console`` 為 None,可只給 file 由本函式新建 Console。
        max_iterations: 防呆上限,避免 mock input 無窮 yield 同字串時 loop 不結束。

    Returns:
        :class:`ReplResult` 描述結束狀態。

    Notes:
        本函式吞掉所有指令層的 :class:`ValueError`(找不到 call_id / 仍有 pending
        ...),改用 ``rich`` 印錯誤,讓 REPL 不會因為 user 拼錯而 crash。其他
        非預期 exception 會原樣 raise。
    """
    ctrl = controller or HITLController()
    ask = input_provider or input
    out = console or Console(file=file)

    _print_state(state, out)
    out.print(Text("Type 'help' for commands.", style="dim"))

    modified = False
    quit_requested = False
    resumed = False

    for _ in range(max_iterations):
        try:
            raw = ask("(hitl) > ")
        except EOFError:
            # ``input`` 被 stub 用完時 EOF 視同 quit。
            quit_requested = True
            break

        verb, args = _parse_command(raw)
        if verb == "":
            continue

        if verb in ("quit", "exit"):
            quit_requested = True
            break

        if verb == "help":
            _print_help(out)
            continue

        if verb == "status":
            _print_state(state, out)
            continue

        if verb == "approve":
            if not args:
                out.print(Text("approve: 缺少 call_id", style="red"))
                continue
            call_id = args[0]
            try:
                ctrl.approve(state, call_id)
            except ValueError as exc:
                out.print(Text(str(exc), style="red"))
                continue
            store.save(state)
            modified = True
            out.print(
                Text(f"approved call_id={call_id}", style="green")
            )
            continue

        if verb == "deny":
            if len(args) < 2:
                out.print(
                    Text("deny: 用法 'deny <call_id> <reason...>'", style="red")
                )
                continue
            call_id = args[0]
            reason = " ".join(args[1:])
            try:
                ctrl.deny(state, call_id, reason)
            except ValueError as exc:
                out.print(Text(str(exc), style="red"))
                continue
            store.save(state)
            modified = True
            out.print(
                Text(f"denied call_id={call_id} reason={reason!r}", style="red")
            )
            continue

        if verb == "continue":
            try:
                ctrl.resume(state)
            except ValueError as exc:
                out.print(Text(str(exc), style="red"))
                continue
            store.save(state)
            modified = True
            resumed = True
            out.print(
                Text(
                    f"resumed run_id={state.run_id} (status={state.status})",
                    style="green",
                )
            )
            break

        out.print(Text(f"unknown command: {verb!r} (try 'help')", style="red"))

    return ReplResult(
        state=state,
        modified=modified,
        quit_requested=quit_requested,
        resumed=resumed,
    )


__all__ = [
    "InputProvider",
    "ReplResult",
    "run_approval_repl",
]
