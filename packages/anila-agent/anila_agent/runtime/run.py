"""Runner 薄包裝：固定帶上 run-context，集中 max_turns 等預設。

P2 會在此加入 RunState persist/resume（HITL 暫停/恢復）輔助。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agents import Runner

from anila_agent.runtime.agent_factory import AssembledAgent

if TYPE_CHECKING:  # 僅型別用，避免在無 session 後端時硬相依
    from agents import RunHooks, RunResult, RunResultStreaming
    from agents.memory import Session

DEFAULT_MAX_TURNS = 10


async def run_once(
    assembled: AssembledAgent,
    user_input: str | list,
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


def run_streamed(
    assembled: AssembledAgent,
    user_input: str | list,
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
