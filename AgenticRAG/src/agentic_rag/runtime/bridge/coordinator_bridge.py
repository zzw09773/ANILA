"""Bridge from AgenticRAG ``AgentDefinition`` to framework ``Coordinator``.

AgenticRAG already has ``coordinator/coordinator.py`` driving workers
through the legacy QueryEngine. This bridge offers a parallel path
that runs each worker through the framework's ``Runner`` instead, so
forks that have migrated their workers to the framework can use the
new Coordinator without rewriting the existing AgentDefinition shape.

What this bridges:

- AgenticRAG ``AgentDefinition`` (agent_type / system_prompt / model
  / tools / max_turns) → framework ``Agent``
- Framework ``Coordinator`` exposed with the same spawn-and-track
  semantics the legacy class offers
- Optional automatic mapping of ``permission_mode`` → ``parallel_safe``

What it does NOT do:

- Replace the legacy coordinator. Both coexist during v0.2; pick one
  based on which runtime your worker agents target.
- Provide the SendMessage / TaskStop protocol (a worker-resume
  feature in the legacy coordinator). v0.2 / Sprint 6 may bring this
  into the framework via BG_TASK ActionKind.

Usage::

    from agentic_rag.runtime.bridge.coordinator_bridge import (
        build_framework_coordinator,
    )

    coord = build_framework_coordinator(
        agent_definitions={"verifier": agent_def, "summariser": ...},
        provider=p,
        # Per-type Action lists for the worker agents:
        actions_by_type={"verifier": [vector_search_action], "summariser": []},
    )
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Optional, Union

from agentic_rag.models.agent import AgentDefinition, PermissionMode
from agentic_rag.runtime.framework import (
    Action,
    Agent,
    Coordinator,
    ModelSettings,
)
from agentic_rag.runtime.framework.middleware.protocol import (
    Middleware,
    MiddlewareCallable,
)
from agentic_rag.runtime.framework.providers.protocol import LLMProvider


def agent_def_to_framework_agent(
    definition: AgentDefinition,
    *,
    provider: LLMProvider,
    actions: Sequence[Action] = (),
    default_model: str = "",
) -> Agent:
    """Map one ``AgentDefinition`` to a framework ``Agent``.

    Falls back to ``default_model`` when the definition's ``model``
    override is None — caller-side concern of "what to use when the
    YAML doesn't pin a specific model."
    """
    model = definition.model or default_model
    if not model:
        raise ValueError(
            f"AgentDefinition {definition.agent_type!r} has no model and no "
            "default_model was provided"
        )
    return Agent(
        name=definition.agent_type,
        instructions=definition.system_prompt,
        provider=provider,
        model=model,
        actions=tuple(actions),
        max_turns=definition.max_turns,
        model_settings=ModelSettings(),
    )


def build_framework_coordinator(
    agent_definitions: dict[str, AgentDefinition],
    *,
    provider: LLMProvider,
    actions_by_type: Optional[dict[str, Sequence[Action]]] = None,
    default_model: str = "",
    middleware: Sequence[Union[Middleware, MiddlewareCallable]] | None = None,
) -> Coordinator:
    """Construct a framework ``Coordinator`` from AgenticRAG defs.

    ``actions_by_type`` is the per-worker Action set — typically the
    RAG action subset relevant to that worker. Defaults to no actions
    (workers that do pure-LLM reasoning).

    Returned Coordinator's worker registry is keyed by ``agent_type``,
    matching the legacy convention so coordinator-tool prompts remain
    drop-in compatible.
    """
    actions_by_type = actions_by_type or {}
    workers: dict[str, Agent] = {}
    for agent_type, definition in agent_definitions.items():
        actions = actions_by_type.get(agent_type, ())
        workers[agent_type] = agent_def_to_framework_agent(
            definition,
            provider=provider,
            actions=actions,
            default_model=default_model,
        )
    return Coordinator(workers=workers, middleware=middleware)


def is_parallel_safe(definition: AgentDefinition) -> bool:
    """Convenience: read-only permission_mode → parallel-safe.

    Mirrors the legacy coordinator's read-only-vs-write classification
    so callers can plumb the same flag through to ``spawn_worker``::

        coord.spawn_worker(
            agent_type, prompt,
            parallel_safe=is_parallel_safe(definitions[agent_type]),
        )
    """
    return definition.permission_mode is PermissionMode.READ_ONLY


__all__ = [
    "agent_def_to_framework_agent",
    "build_framework_coordinator",
    "is_parallel_safe",
]
