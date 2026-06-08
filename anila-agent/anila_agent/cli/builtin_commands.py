"""P2-10 — 10 個 built-in slash command 對應實作。

包含:

* ``/help`` — 列出已註冊 command。
* ``/cost`` — 印 :class:`CostTracker` 摘要(P1-15)。
* ``/budget`` — 印 :class:`BudgetTracker` 使用量(P1-9)。
* ``/trace`` — 印最近 trace 摘要(P0-9)。
* ``/tools`` — 列 active tool / deferred tool 計數(P1-18)。
* ``/find`` — 模糊找檔(P2-7)。
* ``/task`` — list / start 背景 task(P2-12)。
* ``/memory`` — 印 :class:`SessionMemory` 摘要(P2-6)。
* ``/graph`` — 印 agent graph ASCII tree(P2-13)。
* ``/clear`` — 呼叫 ``ctx.history_clearer`` 清掉 caller 自家 history。

每個 command 都做了「對應子系統 None 時回友善 message,不 crash」的容錯設計 —
框架本身不應綁子系統一定要在。
"""

from __future__ import annotations

import logging
from typing import Any

from anila_agent.cli.slash_commands import (
    SlashCommand,
    SlashCommandContext,
    SlashCommandRegistry,
    default_registry,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------


def _make_help_command(registry: SlashCommandRegistry) -> SlashCommand:
    """``/help`` 印出 ``registry`` 內所有 command 名稱 + description。

    抽 factory 是因為 ``/help`` 需要拿到自己所在的 registry — 不能用全域 default,
    否則 caller 自己起一個 isolated registry 時 ``/help`` 會看錯 list。
    """

    def _cb(_args: list[str], _ctx: SlashCommandContext) -> str:
        lines = ["Available slash commands:"]
        for cmd in registry.values():
            lines.append(f"  /{cmd.name:<10} {cmd.description}")
        return "\n".join(lines)

    return SlashCommand(name="help", description="列出所有 slash command", callback=_cb)


# ---------------------------------------------------------------------------
# /cost
# ---------------------------------------------------------------------------


def cmd_cost(_args: list[str], ctx: SlashCommandContext) -> str:
    """印 session cost summary(P1-15 CostTracker)。"""
    tracker = ctx.cost_tracker
    if tracker is None:
        return "no cost tracker active (P1-15 CostTracker 未注入)"
    try:
        return str(tracker.summary_str())
    except AttributeError:
        return f"unsupported cost tracker type: {type(tracker).__name__}"


# ---------------------------------------------------------------------------
# /budget
# ---------------------------------------------------------------------------


def cmd_budget(_args: list[str], ctx: SlashCommandContext) -> str:
    """印 token budget 用量(P1-9 BudgetTracker)。"""
    budget = ctx.budget_tracker
    if budget is None:
        return "no token budget active (P1-9 BudgetTracker 未注入)"
    try:
        used = budget.used
        max_t = budget.max_tokens
        remaining = budget.remaining
        ratio = budget.usage_ratio
    except AttributeError:
        return f"unsupported budget tracker type: {type(budget).__name__}"
    return (
        f"budget: {used}/{max_t} tokens used "
        f"({ratio:.1%}), {remaining} remaining"
    )


# ---------------------------------------------------------------------------
# /trace
# ---------------------------------------------------------------------------


def _collect_recent_traces(tracer: Any, limit: int) -> list[Any]:
    """從 Tracer 的內部 chain index 撈最近 trace(不依賴 public method)。

    Tracer 目前沒暴露 "所有 trace" 的 listing,只有 ``find_traces_by_chain``。
    為了 ``/trace`` 不要依賴 chain_id,直接從 ``_traces_by_chain`` 內取所有
    list 拼起來,按 start_time 倒序取 ``limit`` 筆。

    這是 read-only 訪問內部欄位 — 接受耦合風險,因 Tracer 是同 package 模組,
    後續若加 public ``recent_traces()`` 可直接收斂。
    """
    chains: dict[str, list[Any]] | None = getattr(tracer, "_traces_by_chain", None)
    if not chains:
        return []
    all_traces: list[Any] = []
    for traces in chains.values():
        all_traces.extend(traces)
    # 依 start_time 倒序;若沒 start_time 視為 epoch min(永遠最舊)。
    all_traces.sort(key=lambda t: getattr(t, "start_time", 0) or 0, reverse=True)
    return all_traces[:limit]


def cmd_trace(args: list[str], ctx: SlashCommandContext) -> str:
    """印最近 N 條 trace 摘要(P0-9 Tracer)。

    用法:
        ``/trace`` — 印最近 5 條
        ``/trace 10`` — 印最近 10 條
    """
    tracer = ctx.tracer
    if tracer is None:
        return "no tracer active (P0-9 Tracer 未注入)"
    try:
        limit = int(args[0]) if args else 5
    except ValueError:
        return f"invalid trace limit: {args[0]!r} (need int)"
    limit = max(limit, 1)

    traces = _collect_recent_traces(tracer, limit)
    if not traces:
        return "no traces recorded yet"

    lines = [f"recent traces (top {len(traces)}):"]
    for t in traces:
        # 防呆:不假設 attr 一定存在(老 trace shape 可能不同)。
        name = getattr(t, "name", "?")
        trace_id = getattr(t, "trace_id", "?")
        depth = getattr(t, "depth", None)
        depth_str = f" d={depth}" if depth is not None else ""
        lines.append(f"  - {name} [{trace_id[:8]}]{depth_str}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /tools
# ---------------------------------------------------------------------------


def cmd_tools(_args: list[str], ctx: SlashCommandContext) -> str:
    """列當前 active tool + deferred tool 計數(P1-18 ToolRegistry)。"""
    registry = ctx.tool_registry
    if registry is None:
        return "no tool registry active (P1-18 ToolRegistry 未注入)"
    try:
        active = registry.find_active(session=ctx.session)
        deferred = registry.find_deferred(session=ctx.session)
    except (AttributeError, TypeError):
        return f"unsupported tool registry type: {type(registry).__name__}"

    lines = [
        f"active tools ({len(active)}):",
    ]
    for tool in active:
        name = getattr(tool, "name", "?")
        desc = getattr(tool, "description", "") or ""
        # 截 description 至單行
        desc_one_line = desc.split("\n", 1)[0][:80]
        lines.append(f"  - {name}: {desc_one_line}")
    lines.append(f"deferred tools: {len(deferred)} (未啟用,可由 ToolSearch 啟用)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /find
# ---------------------------------------------------------------------------


def cmd_find(args: list[str], ctx: SlashCommandContext) -> str:
    """模糊找檔(P2-7 FileIndex)。

    用法:``/find auth`` — 列出索引內 path 含 ``auth`` 的最相關 10 筆。
    """
    if not args:
        return "usage: /find <pattern>"
    pattern = " ".join(args)
    index = ctx.file_index
    if index is None:
        return "no file index active (P2-7 FileIndex 未注入)"
    try:
        # 若 index 尚未 build,fuzzy_search 通常會 return 空 — 但 FileIndex
        # 要 caller 先呼 build();這裡不主動 build 避免大目錄 latency。
        matches = index.fuzzy_search(pattern, top_k=10)
    except (AttributeError, TypeError):
        return f"unsupported file index type: {type(index).__name__}"
    if not matches:
        return f"no matches for {pattern!r}"
    lines = [f"matches for {pattern!r} (top {len(matches)}):"]
    for m in matches:
        path = getattr(m, "path", None)
        score = getattr(m, "score", None)
        path_str = str(path) if path is not None else str(m)
        score_str = f" (score={score:.2f})" if isinstance(score, (int, float)) else ""
        lines.append(f"  - {path_str}{score_str}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /task
# ---------------------------------------------------------------------------


async def cmd_task(args: list[str], ctx: SlashCommandContext) -> str:
    """list / start 背景 task(P2-12 TaskManager)。

    用法:
        ``/task list`` — 列出所有 task。
        ``/task start <type> <description>`` — 啟動已 ``register_task_type``
            的 task type;type 不存在則拒絕。

    安全性:**禁止透過 slash 任意執行 callable** — 只允許「已預先 register 過」
    的 task type;對齊 P2-12 ``register_task_type`` 白名單機制。
    """
    manager = ctx.task_manager
    if manager is None:
        return "no task manager active (P2-12 TaskManager 未注入)"

    sub = args[0] if args else "list"

    if sub == "list":
        try:
            tasks = await manager.list_tasks()
        except (AttributeError, TypeError):
            return f"unsupported task manager type: {type(manager).__name__}"
        if not tasks:
            return "no background tasks"
        lines = [f"background tasks ({len(tasks)}):"]
        for t in tasks:
            tid = getattr(t, "task_id", "?")
            state = getattr(t, "state", "?")
            desc = getattr(t, "description", "")
            state_val = getattr(state, "value", state)
            lines.append(f"  - {tid[:8]} [{state_val}] {desc}")
        return "\n".join(lines)

    if sub == "start":
        if len(args) < 2:
            return "usage: /task start <type> [description...]"
        task_type = args[1]
        description = " ".join(args[2:]) if len(args) > 2 else task_type

        # 從 P2-12 process-wide registry 撈 type;非 registered 直接拒絕。
        try:
            from anila_agent.tools.task import get_registered_task_types
        except ImportError:
            return "task type registry unavailable"

        registered = get_registered_task_types()
        if task_type not in registered:
            return (
                f"unknown task type: {task_type!r}; "
                f"registered={list(registered)} "
                f"(use register_task_type() to add)"
            )

        # 從 registry 拿 coro_factory;這裡為了避免循環依賴,直接 access
        # 同模組底層 dict。
        from anila_agent.tools.task import _TASK_TYPE_REGISTRY

        coro_factory = _TASK_TYPE_REGISTRY.get(task_type)
        if coro_factory is None:
            return f"task type {task_type!r} no longer registered"

        try:
            task_id = await manager.start(
                description,
                coro_factory,
                metadata={"type": task_type, "source": "slash"},
            )
        except Exception as exc:
            return f"failed to start task: {type(exc).__name__}: {exc}"
        return f"started task {task_id} (type={task_type})"

    return "usage: /task [list|start <type> <description>]"


# ---------------------------------------------------------------------------
# /memory
# ---------------------------------------------------------------------------


def _format_one_memory(mem: Any) -> str:
    """格式化單個 SessionMemory 摘要(防呆,attr 不存在則跳過)。"""
    session_id = getattr(mem, "session_id", "?")
    summary = getattr(mem, "summary", "") or ""
    topics = getattr(mem, "topics", ()) or ()
    msg_count = getattr(mem, "message_count", 0)
    topics_str = ", ".join(topics) if topics else "(none)"
    summary_brief = summary[:200] + ("..." if len(summary) > 200 else "")
    return (
        f"session {session_id} "
        f"({msg_count} msgs) topics=[{topics_str}]\n"
        f"  {summary_brief}"
    )


def cmd_memory(_args: list[str], ctx: SlashCommandContext) -> str:
    """印 session memory 摘要(P2-6 SessionMemory)。

    ``ctx.session_memory`` 可為單一 :class:`SessionMemory` 或 list — 都支援。
    """
    mem = ctx.session_memory
    if mem is None:
        return "no session memory available (P2-6 SessionMemory 未注入)"
    # 容錯:list / tuple → 多筆;單個物件 → 一筆。
    if isinstance(mem, (list, tuple)):
        if not mem:
            return "no session memory entries"
        lines = [f"session memory ({len(mem)} entries):"]
        for m in mem:
            lines.append(_format_one_memory(m))
        return "\n".join(lines)
    return _format_one_memory(mem)


# ---------------------------------------------------------------------------
# /graph
# ---------------------------------------------------------------------------


def cmd_graph(_args: list[str], ctx: SlashCommandContext) -> str:
    """印 agent graph 的 ASCII tree(P2-13 draw_graph_ascii)。"""
    agent = ctx.agent
    if agent is None:
        return "no agent provided (P2-13 visualization 需注入 root agent)"
    try:
        from anila_agent.extensions.visualization import draw_graph_ascii
    except ImportError as exc:
        return f"visualization unavailable: {exc}"
    try:
        return draw_graph_ascii(agent)
    except Exception as exc:
        return f"draw_graph_ascii failed: {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# /clear
# ---------------------------------------------------------------------------


async def cmd_clear(_args: list[str], ctx: SlashCommandContext) -> str:
    """呼叫 caller 注入的 ``history_clearer`` 清 conversation。

    callable 可同步 / 非同步;若為 ``None`` 回友善提示。
    """
    clearer = ctx.history_clearer
    if clearer is None:
        return "no history clearer wired (caller 需注入 history_clearer callable)"
    try:
        import inspect

        result = clearer()
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        return f"failed to clear history: {type(exc).__name__}: {exc}"
    return "conversation history cleared"


# ---------------------------------------------------------------------------
# Registry bootstrap
# ---------------------------------------------------------------------------


def register_builtin_commands(
    registry: SlashCommandRegistry | None = None,
    *,
    overwrite: bool = False,
) -> SlashCommandRegistry:
    """把全部 10 個 built-in command 註冊到 ``registry``。

    Args:
        registry: 目標 registry;``None`` 用 :func:`default_registry`。
        overwrite: 允許覆蓋同名 command(同 :meth:`SlashCommandRegistry.add`)。

    Returns:
        SlashCommandRegistry: 註冊好後的 registry(同 ``registry`` 入參,方便鏈式)。
    """
    target = registry if registry is not None else default_registry()

    # /help 用 factory(它需要看自家 registry)。
    target.add(_make_help_command(target), overwrite=overwrite)

    target.add(
        SlashCommand("cost", "顯示 session cost summary", cmd_cost),
        overwrite=overwrite,
    )
    target.add(
        SlashCommand("budget", "顯示 token budget 用量", cmd_budget),
        overwrite=overwrite,
    )
    target.add(
        SlashCommand("trace", "顯示最近 trace 摘要 (/trace [N])", cmd_trace),
        overwrite=overwrite,
    )
    target.add(
        SlashCommand("tools", "列當前 active tools", cmd_tools),
        overwrite=overwrite,
    )
    target.add(
        SlashCommand("find", "模糊找檔 (/find <pattern>)", cmd_find),
        overwrite=overwrite,
    )
    target.add(
        SlashCommand("task", "管理背景 task (/task list|start)", cmd_task),
        overwrite=overwrite,
    )
    target.add(
        SlashCommand("memory", "顯示 session memory 摘要", cmd_memory),
        overwrite=overwrite,
    )
    target.add(
        SlashCommand("graph", "印 agent graph ASCII tree", cmd_graph),
        overwrite=overwrite,
    )
    target.add(
        SlashCommand("clear", "清掉 conversation history", cmd_clear),
        overwrite=overwrite,
    )

    return target


def build_default_registry() -> SlashCommandRegistry:
    """建立一個全新且已註冊 10 個 builtin 的 registry。

    與 :func:`register_builtin_commands` 不同:本 helper **不**動 default 全域
    registry,適合 REPL / pytest 想要 isolated registry 的場景。
    """
    registry = SlashCommandRegistry()
    register_builtin_commands(registry)
    return registry


__all__ = [
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
