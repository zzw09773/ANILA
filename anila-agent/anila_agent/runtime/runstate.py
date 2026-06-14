"""HITL 暫停/恢復：RunState 序列化存取，給 web RAG app 跨 HTTP request 用。

流程：
  result = await run_once(assembled, input)
  if has_interruptions(result):
      state = state_from_result(result)
      blob = dump_state(state)            # 連同 SCHEMA_VERSION 存進 DB
      ...（人工核准）...
      state = await load_state(agent, blob)
      approve_all(state, pending_items)   # 或逐一 state.approve(item)
      result = await run_once_state(assembled, state)

注意：持久化的 RunState 綁定 SDK schema 版本（SCHEMA_VERSION）。跨 SDK 升級不可攜——
存檔時記下版本，並對在途（暫停中）的 approval 提供 drain/expire 政策。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agents import RunState
from agents.run_state import CURRENT_SCHEMA_VERSION

if TYPE_CHECKING:
    from agents import Agent

SCHEMA_VERSION = CURRENT_SCHEMA_VERSION


def has_interruptions(result: Any) -> bool:
    """run 結果是否含待核准的 interruption。"""
    return bool(getattr(result, "interruptions", None))


def state_from_result(result: Any) -> RunState:
    """從 run 結果取出可序列化的 RunState。"""
    return result.to_state()


def dump_state(state: RunState) -> str:
    """序列化 RunState 成字串（存 DB 前；務必連同 SCHEMA_VERSION 一起記）。"""
    return state.to_string()


async def load_state(initial_agent: Agent, state_string: str) -> RunState:
    """從字串還原 RunState（恢復用）。"""
    return await RunState.from_string(initial_agent, state_string)


def approve_all(state: RunState, interruptions: list, *, always: bool = False) -> RunState:
    """核准所有 pending interruption（回傳同一 state，便於串接）。"""
    for item in interruptions:
        state.approve(item, always_approve=always)
    return state
