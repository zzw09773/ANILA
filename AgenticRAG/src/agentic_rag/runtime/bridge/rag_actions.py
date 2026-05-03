"""Wrap AgenticRAG ``ToolDefinition`` factories as framework ``Action`` objects.

The existing ``create_vector_search_tool`` / ``create_keyword_search_tool`` /
``create_read_document_tool`` factories produce AgenticRAG-shaped
``ToolDefinition`` objects whose ``implementation`` is an async callable
``(params: dict) -> dict``. The framework's ``Runner`` consumes
``Action`` objects whose handler is ``(ActionContext) -> ActionResult``.

This module provides:

- ``wrap_tool_definition`` — generic adapter from any AgenticRAG
  ``ToolDefinition`` to a framework ``Action``.
- ``build_vector_search_action`` / ``build_keyword_search_action`` /
  ``build_read_document_action`` — convenience builders that go straight
  from store/embedder/reranker to a registerable ``Action``.

Side-effect mapping is conservative — every read-only RAG action is
``SideEffectClass.PURE`` because it queries data without writing. Callers
constructing custom Actions on top of writable tools should pick
``LOCAL`` / ``NETWORKED`` / ``IRREVERSIBLE`` themselves.
"""

from __future__ import annotations

from typing import Any

from agentic_rag.runtime.framework.action import (
    Action,
    ActionContext,
    ActionKind,
    ActionResult,
    SideEffectClass,
)
from agentic_rag.runtime.framework.exceptions import UserError

from agentic_rag.models.tool import ToolDefinition as RagToolDefinition, ToolSafety
from agentic_rag.providers.reranker import Reranker
from agentic_rag.storage.adapters import AgentScopedPgVectorStore
from agentic_rag.tools import (
    EmbedFn,
    create_keyword_search_tool,
    create_read_document_tool,
    create_vector_search_tool,
)


# ── Generic adapter ─────────────────────────────────────────────────────


_SAFETY_TO_SIDE_EFFECT: dict[ToolSafety, SideEffectClass] = {
    ToolSafety.READ_ONLY: SideEffectClass.PURE,
    ToolSafety.CONCURRENCY_SAFE: SideEffectClass.LOCAL,
    ToolSafety.DESTRUCTIVE: SideEffectClass.IRREVERSIBLE,
}


def wrap_tool_definition(tool_def: RagToolDefinition) -> Action:
    """Lift one AgenticRAG ``ToolDefinition`` into a framework ``Action``.

    The wrapped handler:
      - calls ``tool_def.implementation(ctx.params)``
      - if the result dict has an ``"error"`` key (the AgenticRAG tool
        convention) it surfaces as ``ActionResult.error`` so the LLM
        sees a uniform error shape
      - otherwise the dict becomes ``ActionResult.output``

    Tools without an implementation raise ``UserError`` at wrap time,
    not at handler call time, so misconfiguration surfaces during agent
    construction.
    """
    if tool_def.implementation is None:
        raise UserError(
            f"Tool {tool_def.name!r} has no implementation; cannot wrap as Action."
        )

    impl = tool_def.implementation

    async def _handler(ctx: ActionContext) -> ActionResult:
        try:
            result = await impl(ctx.params)
        except Exception as exc:  # noqa: BLE001
            # Tool authors sometimes raise instead of returning {"error": ...}.
            # Normalise to the framework's error shape so the runner can feed
            # the LLM a consistent recovery message.
            return ActionResult(error=f"{type(exc).__name__}: {exc}")
        if not isinstance(result, dict):
            return ActionResult(error=f"tool returned {type(result).__name__}, expected dict")
        if "error" in result and result.get("error"):
            return ActionResult(error=str(result["error"]))
        return ActionResult(output=result)

    return Action(
        name=tool_def.name,
        description=tool_def.description,
        kind=ActionKind.SYNC_TOOL,
        handler=_handler,
        input_schema=dict(tool_def.input_schema or {}),
        side_effect_class=_SAFETY_TO_SIDE_EFFECT.get(
            tool_def.safety, SideEffectClass.PURE
        ),
    )


# ── Convenience builders for the canonical RAG actions ─────────────────


def build_vector_search_action(
    store: AgentScopedPgVectorStore,
    embedder: EmbedFn,
    *,
    default_top_k: int = 5,
    min_score: float = 0.0,
    reranker: Reranker | None = None,
    rerank_pool_multiplier: int = 3,
    collection_id: int | None = None,
) -> Action:
    """Build the canonical ``vector_search`` Action.

    Thin wrapper over ``create_vector_search_tool`` — same arguments,
    same retrieval semantics. The Action that comes out is registerable
    on any framework ``Agent``.
    """
    tool_def = create_vector_search_tool(
        store=store,
        embedder=embedder,
        default_top_k=default_top_k,
        min_score=min_score,
        reranker=reranker,
        rerank_pool_multiplier=rerank_pool_multiplier,
        collection_id=collection_id,
    )
    return wrap_tool_definition(tool_def)


def build_keyword_search_action(
    store: AgentScopedPgVectorStore,
    *,
    default_top_k: int = 5,
    reranker: Reranker | None = None,
    rerank_pool_multiplier: int = 3,
    collection_id: int | None = None,
    tokenizer: Any = None,
) -> Action:
    """Build the canonical ``keyword_search`` Action."""
    tool_def = create_keyword_search_tool(
        store=store,
        default_top_k=default_top_k,
        reranker=reranker,
        rerank_pool_multiplier=rerank_pool_multiplier,
        collection_id=collection_id,
        tokenizer=tokenizer,
    )
    return wrap_tool_definition(tool_def)


def build_read_document_action(
    store: AgentScopedPgVectorStore,
    *,
    max_chunks: int = 200,
) -> Action:
    """Build the canonical ``read_document`` Action."""
    tool_def = create_read_document_tool(store=store, max_chunks=max_chunks)
    return wrap_tool_definition(tool_def)


__all__ = [
    "build_keyword_search_action",
    "build_read_document_action",
    "build_vector_search_action",
    "wrap_tool_definition",
]
