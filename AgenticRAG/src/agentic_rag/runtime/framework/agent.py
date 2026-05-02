"""Agent — the configured unit of intelligence the runner drives.

Fresh-write per ``docs/anila-agent-framework-architecture.md``. Not a
port of openai-agents-python's 941-LOC ``agent.py``: that file is dense
with Realtime / Responses-API attachments, MCP server lifecycle hooks,
and per-agent guardrail wiring that don't fit our 5-primitive model.

Stage B's ``Agent`` is small on purpose:

- ``name`` / ``instructions`` — identity and system-prompt
- ``actions`` — what it can do (becomes a ``ToolRegistry`` internally)
- ``provider`` — how it talks to the LLM
- ``model`` / ``model_settings`` — provider-call parameters
- ``max_turns`` — runaway-loop guard (the runner enforces it)
- ``handoffs`` — explicit list of agents this one can hand control to
  (these become HANDOFF Actions added to the registry automatically)

What's deliberately absent (returns in later sprints):

- ``input_guardrails`` / ``output_guardrails`` — Sprint 2 (Middleware)
- ``hooks`` / ``RunHooks`` lifecycle — Sprint 2 (Middleware)
- ``mcp_servers`` — Sprint 8
- ``output_type`` (structured outputs) — Sprint 2/3 once provider Protocol widens
- Per-agent ``tool_use_behavior`` policy knobs — bake that into the
  StateMachine in Sprint 3, not as a per-agent flag

The agent itself owns no run state; it's a *configuration* object the
``Runner`` interprets. This keeps ``Agent`` immutable (frozen=True) and
safe to share across concurrent runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentic_rag.runtime.framework.action import Action, ActionContext, ActionKind, ActionResult
from agentic_rag.runtime.framework.exceptions import UserError
from agentic_rag.runtime.framework.providers.protocol import LLMProvider
from agentic_rag.runtime.framework.tool import ToolRegistry


# ── Model settings ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelSettings:
    """Common per-call provider parameters.

    Kept deliberately small — only the fields *every* Chat-Completions
    provider supports. Provider-specific knobs (response_format,
    seed, parallel_tool_calls, etc.) go through ``extra`` which the
    provider can read or ignore.

    Defaults of ``None`` mean "let the provider pick"; the framework
    does not impose its own defaults so behaviour matches a raw
    provider call when nothing is set.
    """

    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    tool_choice: str | None = None
    """``"auto"``, ``"none"``, ``"required"``, or a specific tool name."""
    parallel_tool_calls: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    """Provider-specific kwargs forwarded verbatim to ``chat_completion``."""

    def to_provider_kwargs(self) -> dict[str, Any]:
        """Render as kwargs for ``LLMProvider.chat_completion``.

        ``None`` values are dropped so the provider sees only fields
        the caller explicitly set — letting provider defaults stand.
        """
        kwargs: dict[str, Any] = {}
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if self.top_p is not None:
            kwargs["top_p"] = self.top_p
        if self.max_tokens is not None:
            kwargs["max_tokens"] = self.max_tokens
        if self.tool_choice is not None:
            kwargs["tool_choice"] = self.tool_choice
        if self.parallel_tool_calls is not None:
            kwargs["parallel_tool_calls"] = self.parallel_tool_calls
        kwargs.update(self.extra)
        return kwargs


# ── Agent ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Agent:
    """Immutable configuration of an agent.

    Fields:

    - ``name`` — identifier used in tracing, handoff items, error msgs
    - ``instructions`` — system-prompt text. Callable variant (dynamic
      instructions per run) lands in Sprint 3 alongside StateMachine.
    - ``actions`` — list of ``Action``s this agent can invoke
    - ``provider`` — the ``LLMProvider`` to call. Required.
    - ``model`` — provider-side model identifier (e.g.
      ``"gpt-4o-mini"``, ``"gemma-2-9b-it"``)
    - ``model_settings`` — common per-call knobs
    - ``max_turns`` — hard cap on (LLM call → tool exec) cycles before
      the runner raises ``MaxTurnsExceeded``
    - ``handoffs`` — agents this one can hand off to. Each becomes a
      synthetic HANDOFF action added to the registry automatically;
      the LLM sees them as tools named ``transfer_to_<name>``.
    - ``output_type`` — optional Pydantic model / dataclass / callable
      that the runner uses to validate the assistant's final text once
      no more tool calls are pending. ``None`` means "accept any text".
      Validation failure raises ``OutputValidationError`` (a subclass
      of ``ModelBehaviorError``).

    Construction is the only entry point that does work — we build the
    ``ToolRegistry`` once here so per-run lookup is O(1). Because
    ``Agent`` is frozen we use ``object.__setattr__`` in __post_init__
    for the cached registry, mirroring the standard pattern for
    derived state on frozen dataclasses.
    """

    name: str
    instructions: str
    provider: LLMProvider
    model: str
    actions: tuple[Action, ...] = ()
    model_settings: ModelSettings = field(default_factory=ModelSettings)
    max_turns: int = 10
    handoffs: tuple[Agent, ...] = ()
    output_type: type | None = None
    reflection_enabled: bool = False
    """Opt-in: when True, the StateMachine enters REFLECTING after a no-tool-call
    PLANNING turn, asking the LLM to critique its own answer. Capped by
    ``RunState.max_reflections`` to prevent reflection loops. Default False
    keeps behavior identical to the v0.1 single-pass runner."""

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise UserError("Agent.name must be non-empty")
        if not self.model or not self.model.strip():
            raise UserError("Agent.model must be non-empty")
        if self.max_turns < 1:
            raise UserError(f"Agent.max_turns must be >= 1, got {self.max_turns}")

        # Coerce list inputs to tuples so equality / hashing stay sane
        # for the frozen dataclass.
        if isinstance(self.actions, list):
            object.__setattr__(self, "actions", tuple(self.actions))
        if isinstance(self.handoffs, list):
            object.__setattr__(self, "handoffs", tuple(self.handoffs))

        registry = ToolRegistry()
        for action in self.actions:
            registry.register(action)
        for target in self.handoffs:
            registry.register(_handoff_action_for(target))
        object.__setattr__(self, "_registry", registry)

    @property
    def registry(self) -> ToolRegistry:
        """The bound ``ToolRegistry`` (actions + synthetic handoff actions)."""
        registry: ToolRegistry = self.__dict__["_registry"]
        return registry

    def system_message(self) -> str:
        """The system-prompt text the runner injects at turn 0.

        Today this is just ``instructions`` verbatim. When dynamic
        instructions land (Sprint 3) this method becomes the seam
        runners call so callers can replace ``Agent`` without
        rewriting the runner.
        """
        return self.instructions


# ── Synthetic handoff Actions ───────────────────────────────────────────


def _handoff_tool_name(target_name: str) -> str:
    """Conventional name for the tool the LLM calls to hand off.

    Pattern matches openai-agents (``transfer_to_<agent>``) so devs
    coming from that ecosystem recognise it immediately. Spaces and
    dashes are flattened to underscores because most providers
    enforce ``[a-zA-Z0-9_-]`` on tool names.
    """
    safe = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in target_name)
    return f"transfer_to_{safe}"


def _handoff_action_for(target: Agent) -> Action:
    """Build the synthetic ``HANDOFF`` Action that points at ``target``.

    The handler is a thin closure that returns an ``ActionResult`` with
    ``handoff_target=target``. The runner inspects ``handoff_target``
    on the result and switches active agent. Subsequent stages
    (Middleware, StateMachine) layer richer behaviour on top — this is
    the minimum that lets handoffs work in stage B.
    """

    async def _handoff_handler(ctx: ActionContext) -> ActionResult:  # noqa: ARG001
        # Optional ``reason`` arg from the LLM is preserved in metadata
        # so a tracing middleware can surface it.
        reason = ctx.params.get("reason") if isinstance(ctx.params, dict) else None
        meta = {"handoff_reason": reason} if reason else {}
        return ActionResult(handoff_target=target, metadata=meta)

    return Action(
        name=_handoff_tool_name(target.name),
        description=(
            f"Hand off control to the {target.name!r} agent. Use this when the "
            "user's request is better handled by that agent."
        ),
        kind=ActionKind.HANDOFF,
        handler=_handoff_handler,
        input_schema={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Brief rationale for the handoff (optional).",
                }
            },
        },
    )


# ── Public surface ───────────────────────────────────────────────────────


__all__ = ["Agent", "ModelSettings"]
