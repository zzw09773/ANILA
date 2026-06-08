"""P2-10 slash command framework — 通用註冊 / 解析 / 派發機制。

本模組對應 enhancement roadmap **P2-10**:給 sub-agent runtime 一套「通用 slash
command」框架,讓 REPL / CLI / chat surface 可以在不重寫 dispatch 邏輯的前提下
擴充自家 command。對齊上游 ``openai-agents-python`` 沒有 — 直接 port 自 Claude
Code ``src/commands.ts`` + ``src/commands/*`` 的設計取捨(deep-dive §4.20)。

# 為什麼跟 ``cli/commands.py`` 並存而非取代

舊 ``cli/commands.py`` 是 P0 階段為了 ``cli/app.py`` 寫的 thin shim — 跟
``AnilaRunner.assembled`` 綁很死(``ctx.runner.assembled.short_term/long_term``)。
本模組 ``slash_commands.py`` 是「通用框架」:**不假設 runner shape**,所有 P2
子系統(CostTracker / Tracer / ToolRegistry / FileIndex / TaskManager / Agent
graph / SessionMemory)都透過 :class:`SlashCommandContext` 注入,讓 caller
自由組合。

兩者並存:``cli/app.py`` 仍用舊 dispatch(短期不改 P0 行為);P2-14 ``run_demo_loop``
用本框架(透過 :func:`apply_slash_dispatch` helper)。後續可漸進收斂。

# 設計取捨

* **decorator + class registry**:對齊 Pythonic 寫法,讓 caller 可在自己模組
  ``@slash_command("foo")`` 註冊,程式啟動時 import 一次即生效。
* **同步 / 非同步 callback 都支援**:用 ``inspect.iscoroutinefunction`` 判斷
  自動 await — 對齊上游 SDK ``Tool.invoke`` 寫法。
* **dispatch 永遠回 str | None**:``None`` 表示「這不是 slash command,丟給
  agent」;``str`` 表示「已處理,印這段給 user」。caller 不需要區分 success /
  error,所有錯誤包成 user-facing message。
* **unknown command 不 crash**:回 "unknown command" 字串。對齊 Claude Code
  src 的 graceful-degrade UX 慣例。
* **沒實作 PromptCommand**:上游 TS 有 PromptCommand(args 注入 prompt template
  變新 turn);本版只做 LocalCommand。Prompt 版可後續另起 P2 票補。
"""

from __future__ import annotations

import inspect
import logging
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


SlashCallbackResult = str | Awaitable[str]
"""callback 回傳值:str (sync) 或 Awaitable[str] (async)。"""

SlashCallback = Callable[[list[str], "SlashCommandContext"], SlashCallbackResult]
"""slash callback 簽名:``(args, ctx) -> str | Awaitable[str]``。"""


@dataclass
class SlashCommandContext:
    """注入給 slash callback 的執行情境。

    所有欄位皆為 optional — 不同 caller 注入不同子集(REPL 不一定有 TaskManager,
    pytest stub 可只給一個 CostTracker)。callback 自己決定缺哪個就回友善錯誤,
    不假設整組都在。

    Attributes:
        cost_tracker: P1-15 cost tracker — 給 :func:`builtin_commands.cmd_cost`
            印 USD 總額。``None`` 時 ``/cost`` 回 "no cost tracker active"。
        budget_tracker: P1-9 token budget — 給 ``/budget`` 印 quota / used /
            remaining。``None`` 時 ``/budget`` 提示「沒設定 budget」。
        tracer: P0-9 tracer — 給 ``/trace`` 印近期 trace 摘要。``None`` 時提示。
        tool_registry: P1-18 tool registry — 給 ``/tools`` 列 active tool。
            ``None`` 時提示。
        session: 可選 :class:`SessionContext`(用 ``Any`` 避免 circular import)。
            ``/tools`` 用 session 過濾 deferred tool。
        file_index: P2-7 ``FileIndex`` — 給 ``/find`` 模糊找檔。``None`` 時提示。
        task_manager: P2-12 ``TaskManager`` — 給 ``/task`` 列 / 啟動 task。``None``
            時提示。
        session_memory: P2-6 ``SessionMemory`` snapshot(或一份 list) — 給
            ``/memory`` 印摘要。``None`` 時提示。
        agent: P2-13 root agent — 給 ``/graph`` 印 ASCII tree。``None`` 時提示。
        history_clearer: ``/clear`` 用的 callable;由 caller 提供,呼叫後清掉
            自家 conversation history。``None`` 時提示「無 history 可清」。
        extras: 任意擴充欄位 — 給 caller 自家 command 注入(例如 ``run_id``)。
    """

    # P1-15 cost
    cost_tracker: Any = None
    # P1-9 budget
    budget_tracker: Any = None
    # P0-9 tracing
    tracer: Any = None
    # P1-18 tool registry
    tool_registry: Any = None
    session: Any = None
    # P2-7 file index
    file_index: Any = None
    # P2-12 task manager
    task_manager: Any = None
    # P2-6 session memory snapshot — 一個 SessionMemory 或 list[SessionMemory]
    session_memory: Any = None
    # P2-13 root agent
    agent: Any = None
    # clear callback — caller 自家清除 history 邏輯
    history_clearer: Callable[[], Any] | None = None
    # 任意擴充 — caller 自家 command 用
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SlashCommand:
    """單一 slash command 的 immutable record。

    Attributes:
        name: command 名稱,不含前綴 ``/``。``"help"``、``"cost"`` etc.
        description: 一行 help 字串(``/help`` 會列出)。
        callback: 真正執行的 callable。簽名 ``(args, ctx) -> str | Awaitable[str]``。
    """

    name: str
    description: str
    callback: SlashCallback

    def __post_init__(self) -> None:
        if not self.name or not self.name.isidentifier():
            raise ValueError(
                f"SlashCommand.name 必須為合法 identifier,got {self.name!r}"
            )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class SlashCommandRegistry:
    """slash command 註冊表。

    典型流程:

    ```python
    registry = SlashCommandRegistry()

    @slash_command("hello", "Say hello", registry=registry)
    def cmd_hello(args, ctx):
        return "hi!"

    out = await dispatch("/hello", ctx, registry=registry)
    ```

    或者直接呼 :meth:`add` 註冊一個既有 :class:`SlashCommand`:

    ```python
    registry.add(SlashCommand("foo", "Foo", _foo_cb))
    ```
    """

    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}

    # ------------------------------------------------------------------
    # 註冊
    # ------------------------------------------------------------------

    def add(self, command: SlashCommand, *, overwrite: bool = False) -> None:
        """新增 command。

        Args:
            command: 要加入的 :class:`SlashCommand`。
            overwrite: ``True`` 時允許覆蓋同名 command;``False`` 時 raise
                :class:`ValueError`。

        Raises:
            ValueError: 名稱已存在且 ``overwrite=False``。
        """
        if not overwrite and command.name in self._commands:
            raise ValueError(
                f"slash command {command.name!r} already registered"
            )
        self._commands[command.name] = command

    def remove(self, name: str) -> None:
        """移除 command;不存在則 no-op(對齊 ``dict.pop`` 寬鬆語意)。"""
        self._commands.pop(name, None)

    # ------------------------------------------------------------------
    # 查詢
    # ------------------------------------------------------------------

    def get(self, name: str) -> SlashCommand | None:
        """以名稱取 command;不存在回 ``None``。"""
        return self._commands.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._commands

    def __len__(self) -> int:
        return len(self._commands)

    def names(self) -> list[str]:
        """所有已註冊 command 名稱(已排序)。"""
        return sorted(self._commands)

    def values(self) -> list[SlashCommand]:
        """所有已註冊 command(以 name 排序,確保 ``/help`` 輸出穩定)。"""
        return [self._commands[n] for n in self.names()]


# 預設全域 registry — 給 ``@slash_command`` 沒指定 ``registry=`` 時用。
_default_registry = SlashCommandRegistry()


def default_registry() -> SlashCommandRegistry:
    """取得預設 global registry。

    builtin commands 預設註冊到這裡;測試可呼 :func:`reset_default_registry`
    清掉以隔離。
    """
    return _default_registry


def reset_default_registry() -> None:
    """清空 default registry(測試專用)。"""
    _default_registry._commands.clear()


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------


def slash_command(
    name: str,
    description: str = "",
    *,
    registry: SlashCommandRegistry | None = None,
    overwrite: bool = False,
) -> Callable[[SlashCallback], SlashCallback]:
    """把 callable 註冊成 slash command 的 decorator。

    Args:
        name: command 名稱(不含 ``/``)。
        description: 一行 help 字串。
        registry: 目標 registry;``None`` 用 :func:`default_registry`。
        overwrite: 允許覆蓋同名 command。

    Returns:
        decorator — 被裝飾的 callable 原樣回傳(不包 wrapper),只在 import 時
        順手做註冊。

    Example:
        ```python
        @slash_command("ping", "echo pong")
        def cmd_ping(args, ctx):
            return "pong"
        ```
    """
    target = registry if registry is not None else _default_registry

    def _decorator(callback: SlashCallback) -> SlashCallback:
        target.add(
            SlashCommand(name=name, description=description, callback=callback),
            overwrite=overwrite,
        )
        return callback

    return _decorator


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def is_slash_command(line: str) -> bool:
    """判斷一行 input 是否為 slash command(以 ``/`` 開頭、非空)。

    雙斜線 ``//`` 與單獨 ``/`` 不視為 command — 前者通常是 escape 文法,後者
    是空輸入。
    """
    if not line.startswith("/"):
        return False
    stripped = line[1:].strip()
    if not stripped:
        return False
    return not line.startswith("//")


def parse_slash_line(line: str) -> tuple[str, list[str]]:
    """把一行 ``/cmd arg1 "arg with space"`` 拆成 ``(name, args)``。

    用 :func:`shlex.split` 支援 quoted string;失敗時 fallback 為純空白切。

    Raises:
        ValueError: 不是 slash command,或解析後空命令。
    """
    if not line.startswith("/"):
        raise ValueError(f"not a slash command: {line!r}")
    body = line[1:].strip()
    if not body:
        raise ValueError("empty slash command")
    # shlex 容錯 — 引號未閉合時 fallback split。
    try:
        parts = shlex.split(body)
    except ValueError:
        parts = body.split()
    if not parts:
        raise ValueError("empty slash command after parse")
    return parts[0], parts[1:]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


async def dispatch(
    line: str,
    ctx: SlashCommandContext,
    *,
    registry: SlashCommandRegistry | None = None,
) -> str | None:
    """派發 slash command。

    流程:

    1. 非 slash command(``line`` 不以 ``/`` 開頭或為空)→ 回 ``None``,交給
       上層丟給 agent。
    2. parse 失敗 → 回友善錯誤字串(``handled``)。
    3. command 不存在 → 回 ``"unknown command: /<name>"``。
    4. callback 是 coroutine → ``await``;否則直接 call。
    5. callback raise → 包成 ``"error: <type>: <msg>"`` 字串回傳(不破壞 REPL)。

    Args:
        line: 整行 user input(含 ``/`` 前綴)。
        ctx: 注入給 callback 的 :class:`SlashCommandContext`。
        registry: 要查的 registry;``None`` 用 :func:`default_registry`。

    Returns:
        ``str``: 已處理,字串為要印給 user 的訊息。
        ``None``: 非 slash command,caller 應把 ``line`` 丟給 agent。
    """
    target = registry if registry is not None else _default_registry

    if not is_slash_command(line):
        return None

    try:
        name, args = parse_slash_line(line)
    except ValueError as exc:
        return f"error: {exc}"

    cmd = target.get(name)
    if cmd is None:
        return f"unknown command: /{name} (type /help for list)"

    try:
        result = cmd.callback(args, ctx)
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:  # slash callback 不該破 REPL
        logger.exception("slash command /%s raised", name)
        return f"error: {type(exc).__name__}: {exc}"

    return str(result)


__all__ = [
    "SlashCallback",
    "SlashCallbackResult",
    "SlashCommand",
    "SlashCommandContext",
    "SlashCommandRegistry",
    "default_registry",
    "dispatch",
    "is_slash_command",
    "parse_slash_line",
    "reset_default_registry",
    "slash_command",
]
