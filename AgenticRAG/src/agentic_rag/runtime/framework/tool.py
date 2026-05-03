"""Tool definition + registry — the bridge from ``Action`` to provider.

The framework's primitive is ``Action`` (see ``action.py``). The wire
shape the LLM provider expects is a different thing: an OpenAI-style
``{type: "function", function: {name, description, parameters}}``
dict, the Anthropic-style equivalent, etc.

``ToolDefinition`` is the **provider-facing** shape. ``ToolRegistry``
is what the runner queries to (a) hand the right schema to the
provider for the prompt, and (b) look up the matching ``Action`` when
a tool call comes back.

Generic by design: no OpenAI built-in tool surface (Code Interpreter,
File Search, hosted Web Search). Those are provider extensions, not
core primitives — they'd land in ``providers/openai_responses.py``
later if/when we ship a Responses-API provider.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from agentic_rag.runtime.framework.action import Action, ActionKind
from agentic_rag.runtime.framework.exceptions import UserError


# ── Provider-facing tool definition ─────────────────────────────────────


@dataclass(frozen=True)
class ToolDefinition:
    """The serialisable tool description handed to a provider.

    Mirrors the OpenAI Chat Completions ``tools`` entry shape because
    that's what every Chat-Completions-compatible provider speaks. The
    provider may translate further (Anthropic's ``input_schema`` etc.)
    inside its own adapter — this layer doesn't care.

    ``parameters`` is the raw JSON Schema; we don't validate it here
    because providers each have their own quirks (additionalProperties,
    nullable handling, anyOf support). The author owns schema
    correctness.
    """

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    type: str = "function"

    def to_openai_dict(self) -> dict[str, Any]:
        """OpenAI Chat Completions ``tools`` entry shape.

        Output:
            {
              "type": "function",
              "function": {
                "name": ...,
                "description": ...,
                "parameters": {...}
              }
            }
        """
        return {
            "type": self.type,
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }

    @classmethod
    def from_action(cls, action: Action) -> ToolDefinition:
        """Project an Action's public schema fields onto a ToolDefinition.

        Only ``SYNC_TOOL`` and ``HANDOFF`` Actions become tool defs the
        LLM sees. Background tasks are launched programmatically by
        runtime middleware (Sprint 5+), not by LLM tool calls, so they
        don't appear in the prompt's tool list.
        """
        if action.kind is ActionKind.BG_TASK:
            raise UserError(
                f"Action {action.name!r} is BG_TASK; background tasks are not "
                "exposed as LLM-callable tools. Trigger them via middleware."
            )
        return cls(
            name=action.name,
            description=action.description,
            parameters=dict(action.input_schema or {"type": "object", "properties": {}}),
        )


# ── Registry ────────────────────────────────────────────────────────────


class ToolRegistry:
    """Lookup table mapping tool name → ``Action``.

    Keeps the runner thin: instead of scanning ``agent.actions`` on
    every tool call, the agent's bound registry is queried once per
    invocation. Stage B keeps this class small; Sprint 2's middleware
    framework does NOT reach into the registry — it composes around
    the dispatched Action.

    Names must be unique within a registry. Re-registering raises
    ``UserError`` rather than silently shadowing — ambiguous tool
    routing is the kind of bug you'd rather find at construction time
    than at runtime when the LLM picks the wrong handler.
    """

    def __init__(self, actions: Iterable[Action] = ()) -> None:
        self._by_name: dict[str, Action] = {}
        for action in actions:
            self.register(action)

    def register(self, action: Action) -> None:
        if action.name in self._by_name:
            existing = self._by_name[action.name]
            raise UserError(
                f"Tool name conflict: {action.name!r} is already registered "
                f"(existing kind={existing.kind.value}, new kind={action.kind.value}). "
                "Pick a unique name."
            )
        self._by_name[action.name] = action

    def get(self, name: str) -> Action | None:
        """Return the Action for ``name``, or ``None`` if not registered."""
        return self._by_name.get(name)

    def require(self, name: str) -> Action:
        """Like ``get`` but raises ``UserError`` on miss.

        The runner uses this when the LLM emits a tool call — a missing
        name there means the model hallucinated a tool, which is a
        ``ModelBehaviorError`` at the runner boundary; this method
        gives the runner the precise miss to wrap.
        """
        action = self._by_name.get(name)
        if action is None:
            available = ", ".join(sorted(self._by_name)) or "<none>"
            raise UserError(
                f"No tool registered under name {name!r}. Available: {available}"
            )
        return action

    def llm_visible(self) -> list[Action]:
        """Subset of registered Actions that get exposed to the LLM.

        Excludes ``BG_TASK`` Actions (see ``ToolDefinition.from_action``).
        Order is registration order — providers that surface tools in
        their prompt template generally preserve list order.
        """
        return [
            a for a in self._by_name.values() if a.kind is not ActionKind.BG_TASK
        ]

    def tool_definitions(self) -> list[ToolDefinition]:
        """All LLM-visible Actions as ``ToolDefinition`` objects."""
        return [ToolDefinition.from_action(a) for a in self.llm_visible()]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._by_name

    def __len__(self) -> int:
        return len(self._by_name)

    def __iter__(self) -> Iterator[Action]:
        return iter(self._by_name.values())


# ── Public surface ───────────────────────────────────────────────────────


__all__ = ["ToolDefinition", "ToolRegistry"]
