"""``StateMachine`` — pure-ish phase transitions over ``RunState``.

Architecture spec ``docs/anila-agent-framework-architecture.md`` §3.2.

The current Runner (``runner.py``) is event-stream-driven; it owns
the implicit phase transitions. This module exposes them explicitly
so callers can:

- inspect / log every transition
- checkpoint mid-run, restart, resume from a saved ``RunState``
- replay a run for debugging
- drive multi-agent coordination by stepping multiple state machines
  in lockstep

Each ``step()`` call advances the run by ONE atomic unit:

  - PLANNING    → one LLM call → ACTING (if tool_calls) or DONE / REFLECTING
  - ACTING      → one tool dispatch → OBSERVING
  - OBSERVING   → re-enter ACTING if more pending, else PLANNING (next turn)
  - HANDING_OFF → switch active agent → PLANNING (new agent)
  - REFLECTING  → one critique LLM call → PLANNING (revise) or DONE (accept)
  - DONE / ERROR → no-op (returns the same state)

This granularity makes resume cheap: any state can be the next
``step()`` input. The Runner's loop becomes::

    while not state.is_terminal:
        state = await machine.step(state, agent, ...)

The same ``step`` call signature works for first-time runs and
resumed runs — there's no special bootstrap path.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Sequence
from dataclasses import replace
from typing import Any, Union

from agentic_rag.runtime.framework.action import (
    ActionContext,
    ActionKind,
    ActionResult,
)
from agentic_rag.runtime.framework.agent import Agent
from agentic_rag.runtime.framework.exceptions import (
    MaxTurnsExceeded,
    ModelBehaviorError,
    OutputValidationError,
    RunCancelled,
    UserError,
)
from agentic_rag.runtime.framework.items import (
    ChatCompletionResponse,
    FinishReason,
    HandoffItem,
    Message,
    MessageOutputItem,
    Role,
    ToolCall,
    ToolCallItem,
    ToolResult,
    ToolResultItem,
)
from agentic_rag.runtime.framework.middleware.protocol import (
    Middleware,
    MiddlewareCallable,
    compose_chain,
)
from agentic_rag.runtime.framework.runner import (
    _validate_structured_output,
    _validate_tool_input,
)
from agentic_rag.runtime.framework.state import (
    PendingToolCall,
    RunPhase,
    RunState,
)


_REFLECTION_INSTRUCTION = (
    "Critique the assistant's last answer. If the answer is complete, "
    "well-grounded in the available tool results, and addresses the user's "
    "request — reply with the single token ACCEPT. Otherwise, briefly "
    "describe what's missing and the assistant will revise."
)
"""Default reflection prompt. Overridable via ``Agent.reflection_prompt``
once that field exists; for v0.1 this is the fixed text."""


_REFLECTION_ACCEPT_MARKER = "ACCEPT"


class StateMachine:
    """Drives ``RunState`` through phases.

    Construction takes the agent registry — handoff Actions point at
    other Agents by name, and on resume we need to reconstruct the
    "active agent" mapping from a name string in the serialised
    state. Pass ``{name: Agent, ...}`` for every agent the run might
    touch (the primary plus all handoff targets).

    Middleware composition mirrors the Runner: run-level middleware
    wraps action-level middleware wraps the handler. Configure once at
    construction; the same chain applies to every ``step()`` call.
    """

    def __init__(
        self,
        agents: dict[str, Agent],
        *,
        middleware: Sequence[Union[Middleware, MiddlewareCallable]] | None = None,
        bg_task_runner: Any = None,
    ) -> None:
        if not agents:
            raise UserError("StateMachine requires at least one agent")
        self._agents = dict(agents)
        self._middleware: list[Union[Middleware, MiddlewareCallable]] = list(
            middleware or []
        )
        self._bg_runner = bg_task_runner

    @property
    def bg_task_runner(self) -> Any:
        if self._bg_runner is None:
            from agentic_rag.runtime.framework.bg_task import BgTaskRunner

            self._bg_runner = BgTaskRunner()
        return self._bg_runner

    @property
    def agents(self) -> dict[str, Agent]:
        return dict(self._agents)

    def add_agent(self, agent: Agent) -> None:
        """Register an additional agent (e.g. discovered handoff target)."""
        self._agents[agent.name] = agent

    # ── The driver ───────────────────────────────────────────────────

    async def step(self, state: RunState) -> RunState:
        """Advance ``state`` by one atomic phase transition.

        Pure modulo I/O — the only side effects are the LLM call (in
        PLANNING / REFLECTING) and the tool handler (in ACTING). Every
        other phase is local computation.

        Cancellation: callers wrap the loop with their own cancel
        signal / deadline check. The state machine itself does not
        time-out; ``state.deadline_at`` is informational and consulted
        by the wrapping Runner.
        """
        if state.is_terminal:
            return state

        if state.turns_completed >= state.max_turns:
            return self._to_error(
                state,
                MaxTurnsExceeded(
                    f"Run {state.run_id} exceeded max_turns={state.max_turns}"
                ),
            )

        match state.phase:
            case RunPhase.PLANNING:
                return await self._planning(state)
            case RunPhase.ACTING:
                return await self._acting(state)
            case RunPhase.OBSERVING:
                return self._observing(state)
            case RunPhase.HANDING_OFF:
                return self._handing_off(state)
            case RunPhase.REFLECTING:
                return await self._reflecting(state)
            case _:
                return state

    # ── Phase handlers ────────────────────────────────────────────────

    async def _planning(self, state: RunState) -> RunState:
        """One LLM call. Returns ACTING with pending_tool_calls or DONE/REFLECTING."""
        agent = self._require_agent(state.agent_name)

        try:
            response = await self._call_llm(agent, list(state.history))
        except (ModelBehaviorError, OutputValidationError) as exc:
            return self._to_error(state, exc)
        except RunCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            return self._to_error(state, exc)

        assistant_msg = response.message
        new_state = state.append_history(assistant_msg).append_items(
            MessageOutputItem(message=assistant_msg, usage=response.usage)
        ).with_usage_added(response.usage)
        new_state = replace(
            new_state, turns_completed=new_state.turns_completed + 1
        )

        if assistant_msg.tool_calls:
            pending = tuple(
                PendingToolCall(call=tc, index=i)
                for i, tc in enumerate(assistant_msg.tool_calls)
            )
            return new_state.with_phase(
                RunPhase.ACTING, pending_tool_calls=pending
            )

        # No tool calls → maybe REFLECTING, else DONE.
        if (
            agent.reflection_enabled
            and new_state.reflection_count < new_state.max_reflections
        ):
            return new_state.with_phase(RunPhase.REFLECTING)

        return self._finalise(new_state, agent, assistant_msg)

    async def _acting(self, state: RunState) -> RunState:
        """Dispatch ONE pending tool call. Returns OBSERVING."""
        if not state.pending_tool_calls:
            # Nothing to dispatch — should not happen in practice; treat
            # as OBSERVING transition so we re-evaluate.
            return state.with_phase(RunPhase.OBSERVING)

        next_pending, *rest = state.pending_tool_calls
        agent = self._require_agent(state.agent_name)

        result_item, action_result = await self._dispatch_tool(
            agent, next_pending.call, list(state.history), state
        )

        tool_msg = Message.tool(
            call_id=next_pending.call.id,
            name=next_pending.call.name,
            content=action_result.output_as_string(),
        )

        new_state = state.append_history(tool_msg).append_items(
            ToolCallItem(call=next_pending.call), result_item
        )
        new_state = replace(new_state, pending_tool_calls=tuple(rest))

        # Handoff — switch active agent on next OBSERVING tick.
        if action_result.handoff_target is not None:
            target_name = action_result.handoff_target.name
            self.add_agent(action_result.handoff_target)  # discovery
            return new_state.append_items(
                HandoffItem(
                    from_agent=agent.name,
                    to_agent=target_name,
                    reason=action_result.metadata.get("handoff_reason"),
                )
            ).with_phase(RunPhase.HANDING_OFF, handoff_target_name=target_name)

        return new_state.with_phase(RunPhase.OBSERVING)

    def _observing(self, state: RunState) -> RunState:
        """Decide whether more tools remain or it's time to plan again."""
        if state.has_pending_tools:
            return state.with_phase(RunPhase.ACTING)
        return state.with_phase(RunPhase.PLANNING)

    def _handing_off(self, state: RunState) -> RunState:
        """Switch active agent and resume PLANNING under the new one."""
        if state.handoff_target_name is None:
            return self._to_error(
                state,
                UserError("HANDING_OFF phase reached with no handoff_target_name"),
            )
        # Verify the target is registered (it should be from _acting,
        # but defensive check helps with checkpoint-resume scenarios).
        self._require_agent(state.handoff_target_name)
        return state.with_phase(
            RunPhase.PLANNING,
            agent_name=state.handoff_target_name,
            handoff_target_name=None,
        )

    async def _reflecting(self, state: RunState) -> RunState:
        """Run one critique pass. ACCEPT → DONE; otherwise feed critique
        back to PLANNING for another attempt."""
        agent = self._require_agent(state.agent_name)

        # Build a one-shot reflection prompt: the conversation so far
        # plus an instruction to critique the last assistant turn.
        critique_request = list(state.history) + [
            Message.user(_REFLECTION_INSTRUCTION)
        ]
        try:
            response = await self._call_llm(agent, critique_request)
        except Exception as exc:  # noqa: BLE001
            # Reflection failure is non-fatal — accept the original answer.
            return self._finalise_after_reflection(state, agent, accept_reason=str(exc))

        critique_text = self._message_text(response.message).strip()
        new_state = state.with_usage_added(response.usage)
        new_state = replace(
            new_state, reflection_count=new_state.reflection_count + 1
        )

        if critique_text.upper().startswith(_REFLECTION_ACCEPT_MARKER):
            return self._finalise_after_reflection(
                new_state, agent, accept_reason="reflector accepted"
            )

        # Inject critique as a user message and let PLANNING revise.
        critique_msg = Message.user(
            f"[reflection] {critique_text}\n\nPlease revise your previous "
            "answer addressing the points above."
        )
        return new_state.append_history(critique_msg).with_phase(RunPhase.PLANNING)

    # ── Helpers ──────────────────────────────────────────────────────

    def _require_agent(self, name: str) -> Agent:
        agent = self._agents.get(name)
        if agent is None:
            raise UserError(
                f"StateMachine has no agent registered under name {name!r}. "
                f"Known: {sorted(self._agents)}"
            )
        return agent

    def _finalise(
        self, state: RunState, agent: Agent, assistant_msg: Message
    ) -> RunState:
        """Wrap up a run: extract final text + run output_type validation."""
        final_text = self._message_text(assistant_msg)
        try:
            parsed = (
                _validate_structured_output(agent, final_text)
                if agent.output_type is not None
                else None
            )
        except OutputValidationError as exc:
            return self._to_error(state, exc)
        return state.with_phase(
            RunPhase.DONE, final_output=final_text, parsed_output=parsed
        )

    def _finalise_after_reflection(
        self, state: RunState, agent: Agent, accept_reason: str
    ) -> RunState:
        """Reflection accepted (or aborted) — finalise the last assistant turn."""
        # Find the most recent assistant message in history; that's the
        # output the reflection just blessed.
        for msg in reversed(state.history):
            if msg.role is Role.ASSISTANT and not msg.tool_calls:
                return self._finalise(state, agent, msg)
        # Defensive — no usable assistant turn found.
        return self._to_error(
            state,
            UserError(
                f"REFLECTING phase couldn't find a final assistant message "
                f"({accept_reason})"
            ),
        )

    def _to_error(self, state: RunState, exc: BaseException) -> RunState:
        return state.with_phase(
            RunPhase.ERROR,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )

    @staticmethod
    def _message_text(message: Message) -> str:
        if isinstance(message.content, str):
            return message.content
        parts = []
        for part in message.content:
            text = getattr(part, "text", None)
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)

    @staticmethod
    async def _call_llm(
        agent: Agent, history: list[Message]
    ) -> ChatCompletionResponse:
        """Same provider-call shape the Runner uses; lifted here so the
        StateMachine doesn't import from runner.py and risk a cycle."""
        tools = agent.registry.tool_definitions()
        kwargs = agent.model_settings.to_provider_kwargs()
        response = await agent.provider.chat_completion(
            messages=history,
            tools=tools or None,
            model=agent.model,
            stream=False,
            **kwargs,
        )
        if not isinstance(response, ChatCompletionResponse):
            raise ModelBehaviorError(
                f"Provider {type(agent.provider).__name__} returned "
                f"{type(response).__name__}, expected ChatCompletionResponse"
            )
        if response.message.role is not Role.ASSISTANT:
            raise ModelBehaviorError(
                f"Provider returned message with role={response.message.role}, "
                "expected assistant"
            )
        if (
            response.finish_reason is FinishReason.TOOL_CALLS
            and not response.message.tool_calls
        ):
            raise ModelBehaviorError(
                "Provider reported finish_reason=tool_calls but emitted no tool_calls"
            )
        return response

    async def _dispatch_tool(
        self,
        agent: Agent,
        call: ToolCall,
        history: list[Message],
        state: RunState,
    ) -> tuple[ToolResultItem, ActionResult]:
        """Resolve the Action, validate input, run the middleware chain."""
        action = agent.registry.get(call.name)
        if action is None:
            err = ActionResult(
                error=f"Unknown tool {call.name!r}. "
                f"Available: {', '.join(sorted(a.name for a in agent.registry))}"
            )
            return (
                ToolResultItem(
                    result=ToolResult(call_id=call.id, name=call.name, error=err.error),
                    elapsed_seconds=0.0,
                ),
                err,
            )

        if action.kind is ActionKind.BG_TASK:
            try:
                params = call.parsed_arguments()
            except ValueError as exc:
                err = ActionResult(error=str(exc))
                return (
                    ToolResultItem(
                        result=ToolResult(
                            call_id=call.id, name=call.name, error=err.error
                        ),
                        elapsed_seconds=0.0,
                    ),
                    err,
                )
            base_metadata: dict[str, Any] = dict(state.trace_metadata)
            if state.parent_run_id is not None:
                base_metadata["_parent_run_id"] = state.parent_run_id
            if state.group_id is not None:
                base_metadata["_group_id"] = state.group_id
            bg_ctx = ActionContext(
                run_id=state.run_id,
                agent_name=agent.name,
                params=params,
                history=tuple(history),
                metadata=base_metadata,
            )
            handle = self.bg_task_runner.spawn(action, bg_ctx)
            output = handle.to_summary()
            return (
                ToolResultItem(
                    result=ToolResult(
                        call_id=call.id, name=call.name, output=str(output)
                    ),
                    elapsed_seconds=0.0,
                ),
                ActionResult(output=output),
            )

        try:
            params = call.parsed_arguments()
        except ValueError as exc:
            err = ActionResult(error=str(exc))
            return (
                ToolResultItem(
                    result=ToolResult(call_id=call.id, name=call.name, error=err.error),
                    elapsed_seconds=0.0,
                ),
                err,
            )

        validation_error = _validate_tool_input(action.input_schema, params)
        if validation_error is not None:
            err = ActionResult(error=f"[input-validation] {validation_error}")
            return (
                ToolResultItem(
                    result=ToolResult(call_id=call.id, name=call.name, error=err.error),
                    elapsed_seconds=0.0,
                ),
                err,
            )

        base_metadata = dict(state.trace_metadata)
        if state.parent_run_id is not None:
            base_metadata["_parent_run_id"] = state.parent_run_id
        if state.group_id is not None:
            base_metadata["_group_id"] = state.group_id

        ctx = ActionContext(
            run_id=state.run_id,
            agent_name=agent.name,
            params=params,
            history=tuple(history),
            metadata=base_metadata,
        )

        chain = list(self._middleware) + list(action.middleware)
        wrapped_handler = compose_chain(action, chain)

        started = time.perf_counter()
        try:
            handler_return = wrapped_handler(ctx)
            if not inspect.isawaitable(handler_return):
                raise UserError(
                    f"Action {action.name!r} handler did not return an awaitable"
                )
            result = await handler_return
        except Exception as exc:  # noqa: BLE001
            elapsed = time.perf_counter() - started
            err = ActionResult(error=f"{type(exc).__name__}: {exc}")
            return (
                ToolResultItem(
                    result=ToolResult(call_id=call.id, name=call.name, error=err.error),
                    elapsed_seconds=elapsed,
                ),
                err,
            )

        if not isinstance(result, ActionResult):
            raise UserError(
                f"Action {action.name!r} handler returned "
                f"{type(result).__name__}, expected ActionResult"
            )

        elapsed = time.perf_counter() - started
        wire_result = ToolResult(
            call_id=call.id,
            name=call.name,
            output=result.output_as_string() if not result.is_error else None,
            error=result.error,
        )
        return ToolResultItem(result=wire_result, elapsed_seconds=elapsed), result


def create_initial_state(
    agent: Agent,
    user_input: str | Message | list[Message],
    *,
    run_id: str | None = None,
    parent_run_id: str | None = None,
    group_id: str | None = None,
    trace_metadata: dict[str, Any] | None = None,
    deadline_at: float | None = None,
) -> RunState:
    """Build the starting ``RunState`` for a fresh run.

    Mirrors the input normalisation Runner.run() performs so callers
    can drive the StateMachine directly without going through Runner.
    Useful for stepping the machine manually (test / debug / replay)
    or capturing a checkpoint immediately, before any I/O happens.
    """
    import uuid

    rid = run_id or f"run_{uuid.uuid4().hex[:16]}"
    seed = _normalize_user_input(user_input)
    history = _with_system_prompt(agent, seed)
    return RunState(
        run_id=rid,
        agent_name=agent.name,
        model=agent.model,
        parent_run_id=parent_run_id,
        group_id=group_id,
        trace_metadata=dict(trace_metadata or {}),
        phase=RunPhase.PLANNING,
        max_turns=agent.max_turns,
        max_reflections=1,
        history=tuple(history),
        deadline_at=deadline_at,
    )


def _normalize_user_input(
    user_input: str | Message | list[Message],
) -> list[Message]:
    if isinstance(user_input, str):
        return [Message.user(user_input)]
    if isinstance(user_input, Message):
        return [user_input]
    if isinstance(user_input, list):
        for msg in user_input:
            if not isinstance(msg, Message):
                raise UserError(
                    f"user_input list must contain Message, got {type(msg).__name__}"
                )
        return list(user_input)
    raise UserError(
        f"user_input must be str | Message | list[Message], got {type(user_input).__name__}"
    )


def _with_system_prompt(agent: Agent, seed: list[Message]) -> list[Message]:
    if any(m.role in (Role.SYSTEM, Role.DEVELOPER) for m in seed):
        return list(seed)
    prompt = agent.system_message()
    if not prompt:
        return list(seed)
    return [Message.system(prompt), *seed]


__all__ = ["StateMachine", "create_initial_state"]
