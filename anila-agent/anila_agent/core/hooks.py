"""Hook surface ported from claude-code-src `types/hooks.ts`.

We expose six events:

    PreToolUse     — fires before a tool executes; can rewrite input or block.
    PostToolUse    — fires after a tool completes; can inject context for the next turn.
    Stop           — fires when the agent produces a final output.
    SessionStart   — fires once per session, before the first turn.
    UserPromptSubmit — fires when the user submits a new message in the REPL.
    PermissionRequest — fires when a tool call needs explicit approval.

These are bridged onto openai-agents `RunHooks` lifecycle callbacks. Hook callbacks return
a `HookOutput`; the runner aggregates them per event with last-writer-wins for `updated_input`
and union semantics for `additional_context`.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Iterable, Sequence

from agents import Agent, RunContextWrapper, RunHooks, Tool
from agents.items import ModelResponse, TResponseInputItem

from anila_agent.core.events import EventBus
from anila_agent.models.schemas import HookOutput


class HookEvent(str, Enum):
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    STOP = "Stop"
    SESSION_START = "SessionStart"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    PERMISSION_REQUEST = "PermissionRequest"


@dataclass(frozen=True)
class PreToolUseInput:
    tool_name: str
    tool_input: dict[str, Any]
    tool_call_id: str | None
    agent_name: str


@dataclass(frozen=True)
class PostToolUseInput:
    tool_name: str
    tool_input: dict[str, Any]
    tool_output: Any
    tool_call_id: str | None
    agent_name: str


@dataclass(frozen=True)
class StopInput:
    agent_name: str
    final_output: Any
    turns_used: int


@dataclass(frozen=True)
class SessionStartInput:
    session_id: str
    agent_name: str


@dataclass(frozen=True)
class UserPromptSubmitInput:
    prompt: str
    session_id: str


HookCallback = Callable[[Any], "HookOutput | Awaitable[HookOutput]"]


@dataclass(frozen=True)
class HookSpec:
    """Declarative hook registration.

    matcher: regex applied to the tool name (PreToolUse/PostToolUse) or "*"
             (event-level events). "*" or empty matches everything.
    callback: sync or async callable returning HookOutput.
    """

    event: HookEvent
    callback: HookCallback
    matcher: str = ".*"


@dataclass
class _AggregatedHookResult:
    block: bool = False
    abort: bool = False
    reason: str | None = None
    stop_reason: str | None = None
    additional_contexts: list[str] | None = None
    updated_input: dict[str, Any] | None = None


class HookRegistry:
    """Holds hook specs and resolves which fire for an event."""

    def __init__(self, specs: Sequence[HookSpec] = ()) -> None:
        self._specs: list[HookSpec] = list(specs)

    def register(self, spec: HookSpec) -> None:
        self._specs.append(spec)

    def specs_for(self, event: HookEvent, tool_name: str | None = None) -> list[HookSpec]:
        import re

        out: list[HookSpec] = []
        for spec in self._specs:
            if spec.event is not event:
                continue
            if tool_name is None or spec.matcher in ("*", "", ".*"):
                out.append(spec)
                continue
            try:
                if re.fullmatch(spec.matcher, tool_name):
                    out.append(spec)
            except re.error:
                # Malformed regex from config — fall back to literal match.
                if spec.matcher == tool_name:
                    out.append(spec)
        return out


async def _invoke(callback: HookCallback, payload: Any) -> HookOutput:
    result = callback(payload)
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, HookOutput):
        raise TypeError(
            f"hook callback {getattr(callback, '__qualname__', callback)} returned "
            f"{type(result).__name__}, expected HookOutput"
        )
    return result


async def fire(
    registry: HookRegistry,
    event: HookEvent,
    payload: Any,
    *,
    tool_name: str | None = None,
    bus: EventBus | None = None,
) -> _AggregatedHookResult:
    """Run every matching hook and aggregate results."""
    agg = _AggregatedHookResult()
    contexts: list[str] = []
    for spec in registry.specs_for(event, tool_name):
        out = await _invoke(spec.callback, payload)
        if bus is not None:
            bus.emit(
                "hook_fired",
                event=event.value,
                callback=getattr(spec.callback, "__qualname__", repr(spec.callback)),
                tool_name=tool_name,
                decision=out.decision,
            )
        if out.continue_ is False:
            agg.abort = True
            agg.stop_reason = out.stop_reason or out.reason
        if out.decision == "block":
            agg.block = True
            agg.reason = out.reason
        if out.additional_context:
            contexts.append(out.additional_context)
        if out.updated_input is not None:
            agg.updated_input = dict(out.updated_input)
    if contexts:
        agg.additional_contexts = contexts
    return agg


class AnilaRunHooks(RunHooks[Any]):
    """Bridge openai-agents lifecycle callbacks into Anila hook events.

    The runner instantiates this with the registry and event bus, then passes it to Runner.run.
    Each lifecycle callback is translated:

        on_tool_start  -> PreToolUse
        on_tool_end    -> PostToolUse
        on_agent_end   -> Stop
        on_llm_*       -> emitted to the event bus (no hook event by default)
    """

    def __init__(
        self,
        registry: HookRegistry,
        bus: EventBus,
        *,
        agent_name: str,
    ) -> None:
        self._registry = registry
        self._bus = bus
        self._agent_name = agent_name
        self._turns = 0

    @property
    def turns(self) -> int:
        return self._turns

    async def on_llm_start(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        system_prompt: str | None,
        input_items: list[TResponseInputItem],
    ) -> None:
        self._turns += 1
        self._bus.emit("llm_started", agent=agent.name, turn=self._turns)

    async def on_llm_end(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        response: ModelResponse,
    ) -> None:
        self._bus.emit("llm_ended", agent=agent.name, turn=self._turns)

    async def on_tool_start(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        tool: Tool,
    ) -> None:
        tool_input, call_id = _extract_tool_call(context)
        payload = PreToolUseInput(
            tool_name=tool.name,
            tool_input=tool_input,
            tool_call_id=call_id,
            agent_name=agent.name,
        )
        self._bus.emit("tool_started", tool=tool.name, agent=agent.name, input=tool_input)
        agg = await fire(
            self._registry, HookEvent.PRE_TOOL_USE, payload, tool_name=tool.name, bus=self._bus
        )
        if agg.abort:
            from agents.exceptions import UserError

            raise UserError(agg.stop_reason or "Aborted by PreToolUse hook")
        if agg.block:
            from agents.exceptions import UserError

            raise UserError(agg.reason or f"PreToolUse blocked {tool.name}")
        # updated_input is honoured by mutating tool_arguments on the ToolContext.
        if agg.updated_input is not None and hasattr(context, "tool_arguments"):
            try:
                import json

                context.tool_arguments = json.dumps(agg.updated_input)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass

    async def on_tool_end(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        tool: Tool,
        result: str,
    ) -> None:
        tool_input, call_id = _extract_tool_call(context)
        payload = PostToolUseInput(
            tool_name=tool.name,
            tool_input=tool_input,
            tool_output=result,
            tool_call_id=call_id,
            agent_name=agent.name,
        )
        self._bus.emit("tool_ended", tool=tool.name, agent=agent.name)
        await fire(
            self._registry, HookEvent.POST_TOOL_USE, payload, tool_name=tool.name, bus=self._bus
        )

    async def on_agent_end(
        self,
        context: Any,
        agent: Agent[Any],
        output: Any,
    ) -> None:
        payload = StopInput(agent_name=agent.name, final_output=output, turns_used=self._turns)
        self._bus.emit("turn_ended", agent=agent.name, turns=self._turns)
        await fire(self._registry, HookEvent.STOP, payload, bus=self._bus)


def _extract_tool_call(context: Any) -> tuple[dict[str, Any], str | None]:
    """Pull tool_arguments + tool_call_id from a ToolContext if available."""
    args_raw = getattr(context, "tool_arguments", None)
    call_id = getattr(context, "tool_call_id", None)
    if isinstance(args_raw, str):
        try:
            import json

            parsed = json.loads(args_raw)
            if isinstance(parsed, dict):
                return parsed, call_id
        except Exception:  # noqa: BLE001
            pass
    if isinstance(args_raw, dict):
        return args_raw, call_id
    return {}, call_id


def fire_session_start(
    registry: HookRegistry, bus: EventBus, *, session_id: str, agent_name: str
) -> Awaitable[_AggregatedHookResult]:
    return fire(
        registry,
        HookEvent.SESSION_START,
        SessionStartInput(session_id=session_id, agent_name=agent_name),
        bus=bus,
    )


def fire_user_prompt_submit(
    registry: HookRegistry, bus: EventBus, *, prompt: str, session_id: str
) -> Awaitable[_AggregatedHookResult]:
    return fire(
        registry,
        HookEvent.USER_PROMPT_SUBMIT,
        UserPromptSubmitInput(prompt=prompt, session_id=session_id),
        bus=bus,
    )


def specs_from_config(
    entries: Iterable[dict[str, Any]],
    event: HookEvent,
    *,
    auto_memory_enabled: bool,
) -> list[HookSpec]:
    """Translate `tools.yaml` hook entries into HookSpec objects.

    Entries with `when: auto_memory` are skipped unless auto memory is enabled.
    """
    import importlib

    out: list[HookSpec] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        when = entry.get("when")
        if when == "auto_memory" and not auto_memory_enabled:
            continue
        callback_path = entry.get("callback")
        if not callback_path:
            continue
        module_path, _, attr = callback_path.rpartition(".")
        if not module_path:
            raise ValueError(f"Invalid callback path: {callback_path!r}")
        callback = getattr(importlib.import_module(module_path), attr)
        out.append(HookSpec(event=event, callback=callback, matcher=entry.get("matcher", ".*")))
    return out
