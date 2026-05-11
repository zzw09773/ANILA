"""Agent assembly. Pulls config, tools, model, hooks together into an `Agent` instance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agents import Agent, ModelSettings
from agents.memory.session import Session
from agents.models.interface import Model

from anila_agent.core.events import EventBus
from anila_agent.core.hooks import HookEvent, HookRegistry, HookSpec, specs_from_config
from anila_agent.memory import open_session
from anila_agent.memory import summarizer as summarizer_module
from anila_agent.memory.long_term import LongTermMemory
from anila_agent.memory.store import MemdirStore
from anila_agent.models.openai_compatible import build_model, build_model_settings
from anila_agent.retrieval.anila_pgvector import from_env as _anila_pgvector_from_env
from anila_agent.retrieval.pgvector import from_env as _pgvector_from_env
from anila_agent.tools.rag_tools import set_retriever
from anila_agent.tools.registry import load_tools
from anila_agent.utils.config import AppConfig
from anila_agent.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class AssembledAgent:
    """Everything the runner needs in one place."""

    agent: Agent[Any]
    model_settings: ModelSettings
    hook_registry: HookRegistry
    event_bus: EventBus
    short_term: Session | None
    long_term: LongTermMemory | None
    config: AppConfig

    @property
    def max_turns(self) -> int:
        return self.config.agent.max_turns


def build_agent(
    config: AppConfig,
    *,
    session_id: str = "default",
    extra_tools: list[Any] | None = None,
    extra_hooks: list[HookSpec] | None = None,
) -> AssembledAgent:
    """Construct the full agent graph from configuration.

    Args:
        config: Loaded `AppConfig` (see `utils/config.py`).
        session_id: Short-term session key. Identical IDs resume the same conversation.
        extra_tools: FunctionTools to add on top of those declared in `tools.yaml`.
        extra_hooks: HookSpecs added on top of `tools.yaml` declarations.
    """
    model: Model = build_model(config.model)
    model_settings = build_model_settings(config.model)

    # Prefer the ANILA-native retriever when ANILA_COLLECTION_ID is set;
    # fall back to the langchain_postgres-based one for generic deployments.
    retriever = _anila_pgvector_from_env() or _pgvector_from_env()
    if retriever is not None:
        set_retriever(retriever)
        logger.info("retriever installed: %s", retriever.name)

    builtin_tools = load_tools(config.tools.builtin)
    all_tools = [*builtin_tools, *(extra_tools or [])]

    long_term: LongTermMemory | None = None
    if config.memory.long_term_enabled:
        store = MemdirStore(
            config.memory.long_term_path,
            max_index_lines=config.memory.max_index_lines,
            max_index_bytes=config.memory.max_index_bytes,
            max_files=config.memory.max_files,
        )
        long_term = LongTermMemory(store, model=model, model_settings=ModelSettings(
            temperature=0.0, max_tokens=512,
        ))

    summarizer_module.configure(
        memory=long_term,
        model=model,
        enabled=config.memory.auto_memory_enabled,
        min_messages_between_runs=config.memory.auto_memory_min_messages,
    )

    short_term: Session | None = None
    if config.memory.short_term_enabled:
        short_term = open_session(session_id, config.memory.short_term_path)

    hook_specs: list[HookSpec] = []
    hook_specs.extend(
        specs_from_config(
            config.tools.pre_tool_use,
            HookEvent.PRE_TOOL_USE,
            auto_memory_enabled=config.memory.auto_memory_enabled,
        )
    )
    hook_specs.extend(
        specs_from_config(
            config.tools.post_tool_use,
            HookEvent.POST_TOOL_USE,
            auto_memory_enabled=config.memory.auto_memory_enabled,
        )
    )
    hook_specs.extend(
        specs_from_config(
            config.tools.stop,
            HookEvent.STOP,
            auto_memory_enabled=config.memory.auto_memory_enabled,
        )
    )
    if extra_hooks:
        hook_specs.extend(extra_hooks)

    registry = HookRegistry(hook_specs)
    bus = EventBus()

    instructions = config.agent.instructions
    if long_term is not None:
        index_text = long_term.list_index().strip()
        if index_text:
            instructions = (
                f"{instructions}\n\n## MEMORY.md (your persistent index)\n\n{index_text}"
            )

    agent = Agent[Any](
        name=config.agent.name,
        instructions=instructions,
        tools=all_tools,
        model=model,
        model_settings=model_settings,
        tool_use_behavior=config.agent.tool_use_behavior,  # type: ignore[arg-type]
    )

    return AssembledAgent(
        agent=agent,
        model_settings=model_settings,
        hook_registry=registry,
        event_bus=bus,
        short_term=short_term,
        long_term=long_term,
        config=config,
    )
