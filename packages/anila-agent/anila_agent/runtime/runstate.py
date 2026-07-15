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

from collections.abc import Mapping
from typing import Any, cast

from agents import Agent, RunState, ToolApprovalItem
from agents.run_state import CURRENT_SCHEMA_VERSION

SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
CONTEXT_SCHEMA = "anila-agent-run-context/v1"


def _serialize_context(_context: Any) -> Mapping[str, Any]:
    """Return only the durable marker for an execution context.

    ``AnilaRunContext`` deliberately contains live process resources (the
    retriever, tracing wrappers and optional memory runtime).  None of those
    objects is a durable input, and serializing their dataclass fields would
    either leak credentials or leave a restart with a dead client.  The
    service rebuilds the complete context for every request and supplies it as
    ``context_override`` when restoring the SDK state.
    """

    return {"schema": CONTEXT_SCHEMA}


def has_interruptions(result: Any) -> bool:
    """run 結果是否含待核准的 interruption。"""
    return bool(getattr(result, "interruptions", None))


def state_from_result(result: Any) -> RunState:
    """從 run 結果取出可序列化的 RunState。"""
    return cast(RunState, result.to_state())


def dump_state(state: RunState) -> str:
    """Serialize a RunState without persisting live context dependencies.

    The OpenAI Agents SDK's context serializer is the supported extension
    point for custom run contexts.  Passing ``strict_context`` keeps this
    boundary fail-closed if the SDK ever changes its custom-context handling.
    """

    return state.to_string(context_serializer=_serialize_context, strict_context=True)


async def load_state(
    initial_agent: Agent,
    state_string: str,
    *,
    context_override: Any,
) -> RunState:
    """Restore state while rebinding a freshly assembled execution context.

    A persisted context marker is intentionally not executable.  Callers must
    provide the new request's ``AnilaRunContext`` so retriever, trace emitter,
    memory and other process-local dependencies cannot survive a restart.
    """

    if context_override is None:
        raise ValueError("durable RunState restore requires a fresh context_override")
    return await RunState.from_string(
        initial_agent,
        state_string,
        context_override=context_override,
        strict_context=True,
    )


def approve_all(
    state: RunState, interruptions: list[ToolApprovalItem], *, always: bool = False
) -> RunState:
    """核准所有 pending interruption（回傳同一 state，便於串接）。"""
    for item in interruptions:
        state.approve(item, always_approve=always)
    return state
