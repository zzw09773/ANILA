"""Factory for the canonical AgenticRAG framework ``Agent``.

Hand it the host's already-resolved store / embedder / reranker (the
same objects ``server.create_app`` plumbs through today) plus the
LLM provider, and you get back a ready-to-run ``Agent`` with
vector_search + keyword_search + read_document Actions registered.

Why this lives in AgenticRAG and not the framework: the framework
deliberately knows nothing about RAG. AgenticRAG owns "what tools a
RAG agent should ship with by default." If a fork wants to add their
own tools they can just register more Actions on top of the base set
the builder returns.
"""

from __future__ import annotations

from typing import Any

from agentic_rag.runtime.framework import Action, Agent, ModelSettings
from agentic_rag.runtime.framework.providers.protocol import LLMProvider

from agentic_rag.providers.reranker import Reranker
from agentic_rag.storage.adapters import AgentScopedPgVectorStore

from agentic_rag.runtime.bridge.rag_actions import (
    build_keyword_search_action,
    build_read_document_action,
    build_vector_search_action,
)


def build_rag_agent(
    *,
    name: str,
    instructions: str,
    provider: LLMProvider,
    model: str,
    store: AgentScopedPgVectorStore | None = None,
    embedder: Any = None,
    reranker: Reranker | None = None,
    extra_actions: list[Action] | None = None,
    max_turns: int = 10,
    temperature: float = 0.0,
    max_tokens: int = 4096,
    default_top_k: int = 5,
    rerank_pool_multiplier: int = 3,
    collection_id: int | None = None,
    tokenizer: Any = None,
    include_keyword_search: bool = True,
    include_read_document: bool = True,
) -> Agent:
    """Assemble a RAG ``Agent`` for the framework runner.

    Arguments mirror the existing ``server.create_app`` injection set
    so call sites can pass-through whatever they already have.

    The three RAG actions are added conditionally:
      - ``vector_search`` requires both ``store`` and ``embedder`` — if
        either is missing the action is skipped.
      - ``keyword_search`` requires ``store`` and is opt-out via
        ``include_keyword_search=False``.
      - ``read_document`` requires ``store`` and is opt-out via
        ``include_read_document=False``.

    ``extra_actions`` is appended last so caller-defined tools live
    alongside the canonical RAG set.
    """
    actions: list[Action] = []

    if store is not None and embedder is not None:
        actions.append(
            build_vector_search_action(
                store=store,
                embedder=embedder,
                reranker=reranker,
                default_top_k=default_top_k,
                rerank_pool_multiplier=rerank_pool_multiplier,
                collection_id=collection_id,
            )
        )

    if store is not None and include_keyword_search:
        actions.append(
            build_keyword_search_action(
                store=store,
                reranker=reranker,
                default_top_k=default_top_k,
                rerank_pool_multiplier=rerank_pool_multiplier,
                collection_id=collection_id,
                tokenizer=tokenizer,
            )
        )

    if store is not None and include_read_document:
        actions.append(build_read_document_action(store=store))

    if extra_actions:
        actions.extend(extra_actions)

    return Agent(
        name=name,
        instructions=instructions,
        provider=provider,
        model=model,
        actions=tuple(actions),
        model_settings=ModelSettings(
            temperature=temperature,
            max_tokens=max_tokens,
        ),
        max_turns=max_turns,
    )


__all__ = ["build_rag_agent"]
