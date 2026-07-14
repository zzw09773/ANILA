"""Runner 薄包裝：固定帶上 run-context，集中 max_turns 等預設。

P2 會在此加入 RunState persist/resume（HITL 暫停/恢復）輔助。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agents import Runner

from anila_agent.runtime.agent_factory import AssembledAgent

if TYPE_CHECKING:  # 僅型別用，避免在無 session 後端時硬相依
    from agents import RunHooks, RunResult, RunResultStreaming, RunState
    from agents.memory import Session

DEFAULT_MAX_TURNS = 10


async def run_once(
    assembled: AssembledAgent,
    user_input: str | list[Any],
    *,
    session: Session | None = None,
    max_turns: int | None = None,
    hooks: RunHooks | None = None,
) -> RunResult:
    """跑完一輪（非串流），回傳 RunResult。max_turns 未給時用 assembled 的值。"""
    return await Runner.run(
        assembled.agent,
        user_input,
        context=assembled.context,
        session=session,
        max_turns=max_turns if max_turns is not None else assembled.max_turns,
        hooks=hooks,
    )


async def run_once_state(
    assembled: AssembledAgent,
    state: RunState,
    *,
    session: Session | None = None,
    hooks: RunHooks | None = None,
) -> RunResult:
    """Resume one persisted SDK ``RunState`` through the same Runner path.

    ``RunState`` carries the original input, generated items, approval state,
    trace state and max-turn budget.  Reusing ``Runner.run`` with the state as
    its input is the SDK-supported resume protocol; no second agent engine is
    introduced here.  The assembled context is supplied so a restarted
    process can rebind the durable state to the freshly built official agent.
    """

    return await Runner.run(
        assembled.agent,
        state,
        context=assembled.context,
        session=session,
        hooks=hooks,
    )


def run_streamed(
    assembled: AssembledAgent,
    user_input: str | list[Any],
    *,
    session: Session | None = None,
    max_turns: int | None = None,
    hooks: RunHooks | None = None,
) -> RunResultStreaming:
    """啟動串流執行，回傳可迭代 stream_events 的結果。

    hooks 與 run_once 對齊（per-user 稽核/計量在串流路徑一樣要掛）。
    """
    return Runner.run_streamed(
        assembled.agent,
        user_input,
        context=assembled.context,
        session=session,
        max_turns=max_turns if max_turns is not None else assembled.max_turns,
        hooks=hooks,
    )
