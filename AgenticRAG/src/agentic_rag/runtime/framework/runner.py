"""Runner — drives an Agent through one user request.

The single-pass loop:

    while turn < max_turns:
        check cancellation (signal / deadline)
        response = provider.chat_completion(history, tools)
        history.append(response.message)
        if no tool_calls: validate output (if output_type set) and return
        for each tool_call:
            check cancellation
            validate input against action.input_schema
            run middleware chain → handler
            history.append(tool message)
        # next loop turn → LLM sees tool results

What this stays away from (deferred to later sprints):

- No StateMachine — Sprint 3 makes phase transitions explicit + checkpointable
- No persistent run state / resume — Sprint 3 (RunState immutable snapshots)
- No parallel tool dispatch — sequential to keep ordering trivial
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
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
    RunItem,
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
from agentic_rag.runtime.framework.stream_events import (
    HandoffEvent,
    MessageDeltaEvent,
    RunCompletedEvent,
    RunErrorEvent,
    StreamEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
    UsageUpdateEvent,
)
from agentic_rag.runtime.framework.usage import Usage


# ── Run result ──────────────────────────────────────────────────────────


@dataclass
class RunResult:
    """What ``Runner.run`` returns.

    Mutable on purpose — middleware metadata may attach here, future
    StateMachine work will add a snapshot pointer. The fields below are
    the stable surface every caller can rely on.

    Correlation fields (``parent_run_id`` / ``group_id`` / ``trace_metadata``)
    propagate from caller through middleware spans so distributed
    traces / multi-agent chains can be reconstructed without scanning
    application logs.

    ``parsed_output`` is populated when ``Agent.output_type`` is set —
    callers asking for structured outputs read this directly instead
    of re-parsing ``final_output``.
    """

    run_id: str
    final_output: str
    final_message: Message
    history: list[Message]
    items: list[RunItem]
    usage: Usage = field(default_factory=Usage)
    final_agent_name: str = ""
    turns: int = 0
    parent_run_id: str | None = None
    group_id: str | None = None
    trace_metadata: dict[str, Any] = field(default_factory=dict)
    parsed_output: Any = None


# ── Runner ──────────────────────────────────────────────────────────────


class Runner:
    """Drives an Agent through one user request.

    Surface: ``run(agent, user_input)`` returns a ``RunResult``.
    Re-entrant — a single ``Runner`` can serve many concurrent runs
    because all state lives on the ``RunResult`` being built up.

    ``middleware`` is the run-level chain applied to every Action
    invocation in this run. It composes outermost-to-innermost in
    list order, then ``Action.middleware`` (per-Action, declared at
    Action construction) wraps the handler innermost. So the
    effective execution order from outside in is:

        runner_mw[0] → runner_mw[1] → … → action_mw[0] → … → handler

    Cancellation: pass ``cancel_signal=asyncio.Event()`` and call
    ``signal.set()`` from another task to ask the runner to stop
    cooperatively at the next safe point. Or pass ``deadline_seconds``
    for an absolute wall-clock cap. Either path raises ``RunCancelled``.
    """

    def __init__(
        self,
        middleware: Sequence[Union[Middleware, MiddlewareCallable]] | None = None,
        *,
        bg_task_runner: Any = None,  # BgTaskRunner — typed Any to avoid import cycle
    ) -> None:
        self._middleware: list[Union[Middleware, MiddlewareCallable]] = list(
            middleware or []
        )
        self._bg_runner = bg_task_runner

    @property
    def middleware(self) -> tuple[Union[Middleware, MiddlewareCallable], ...]:
        return tuple(self._middleware)

    @property
    def bg_task_runner(self) -> Any:
        """Lazy-construct a default in-memory BgTaskRunner if none was injected.

        Callers wanting file-backed sinks / explicit output_dir should
        pass their own at construction. The lazy default keeps the
        common case (Runner() with no kwargs) zero-config.
        """
        if self._bg_runner is None:
            from agentic_rag.runtime.framework.bg_task import BgTaskRunner

            self._bg_runner = BgTaskRunner()
        return self._bg_runner

    def add_middleware(self, mw: Union[Middleware, MiddlewareCallable]) -> None:
        """Append a middleware to the run-level chain.

        Append-only: middleware added later wraps middleware added earlier
        on the inside, matching the "first registered = outermost"
        convention. New middleware applies to subsequent ``run()`` calls;
        runs already in flight keep the chain they started with (we copy
        the list at run start).
        """
        self._middleware.append(mw)

    async def resume_from_state(
        self,
        state: Any,  # RunState — not annotated to avoid the import cycle
        agents: dict[str, Any],
        *,
        cancel_signal: asyncio.Event | None = None,
    ) -> RunResult:
        """Drive a saved ``RunState`` through StateMachine to completion.

        Use case: pod restarted; the previous in-flight run was
        checkpointed via ``RunSerializer.dump(state)``. After restart,
        rebuild ``agents`` (the live Agent objects keyed by name —
        StateMachine uses these for handoff resolution), then call
        ``resume_from_state(state, agents)``.

        Cancellation: the StateMachine itself does not honor signals
        (it's a pure transition function). The Runner-level loop
        checks ``cancel_signal`` between every step.

        Returns a ``RunResult`` matching what ``run()`` would have
        produced if the run hadn't been interrupted. Errors raised by
        the StateMachine surface as ``ERROR`` phase; the Runner
        re-raises them so callers see the same exception model.

        Stage-D first cut intentionally does NOT emit StreamEvents
        (those are tied to the v0.1 stream() loop). Sprint-3 follow-up
        will refactor stream() to share the StateMachine driver, at
        which point both APIs emit identical events.
        """
        # Local imports to avoid a cycle: state_machine imports from
        # runner (the validators), so runner can't import from
        # state_machine at module load.
        from agentic_rag.runtime.framework.state import RunPhase, RunState
        from agentic_rag.runtime.framework.state_machine import StateMachine

        if not isinstance(state, RunState):
            raise UserError(
                f"resume_from_state expected RunState, got {type(state).__name__}"
            )

        machine = StateMachine(agents, middleware=self._middleware)
        current = state

        while not current.is_terminal:
            if cancel_signal is not None and cancel_signal.is_set():
                raise RunCancelled("signal", current.run_id, current.turns_completed)
            current = await machine.step(current)

        if current.phase is RunPhase.ERROR:
            err_type = current.error_type or "AgentsException"
            err_msg = current.error_message or ""
            # Re-raise the original exception class when we know it.
            for exc_cls in (
                MaxTurnsExceeded,
                ModelBehaviorError,
                UserError,
            ):
                if exc_cls.__name__ == err_type:
                    raise exc_cls(err_msg)
            raise ModelBehaviorError(f"{err_type}: {err_msg}")

        # Build a RunResult from the terminal state.
        history_list = list(current.history)
        # final_message is the last assistant message in history.
        final_msg = next(
            (m for m in reversed(history_list) if m.role.value == "assistant"),
            Message.assistant(current.final_output or ""),
        )
        return RunResult(
            run_id=current.run_id,
            final_output=current.final_output or "",
            final_message=final_msg,
            history=history_list,
            items=list(current.items),
            usage=current.usage,
            final_agent_name=current.agent_name,
            turns=current.turns_completed,
            parent_run_id=current.parent_run_id,
            group_id=current.group_id,
            trace_metadata=dict(current.trace_metadata),
            parsed_output=current.parsed_output,
        )

    async def run(
        self,
        agent: Agent,
        user_input: str | Message | list[Message],
        *,
        run_id: str | None = None,
        parent_run_id: str | None = None,
        group_id: str | None = None,
        trace_metadata: dict[str, Any] | None = None,
        cancel_signal: asyncio.Event | None = None,
        deadline_seconds: float | None = None,
    ) -> RunResult:
        """Run ``agent`` to completion and return the final ``RunResult``.

        Convenience wrapper around ``stream()``: drains every event,
        accumulates the result, and returns it. Use ``stream()`` directly
        when you need incremental events (SSE / WebSocket frontends);
        use ``run()`` when you only care about the final answer.

        See ``stream()`` for the full kwarg semantics — they're identical.
        """
        result_holder: dict[str, Any] = {}
        async for event in self.stream(
            agent,
            user_input,
            run_id=run_id,
            parent_run_id=parent_run_id,
            group_id=group_id,
            trace_metadata=trace_metadata,
            cancel_signal=cancel_signal,
            deadline_seconds=deadline_seconds,
            _capture=result_holder,
        ):
            # Re-raise terminal errors so callers can use try/except
            # rather than always inspecting events.
            if isinstance(event, RunErrorEvent):
                raise result_holder.pop("error")
        result: RunResult = result_holder["result"]
        return result

    async def stream(
        self,
        agent: Agent,
        user_input: str | Message | list[Message],
        *,
        run_id: str | None = None,
        parent_run_id: str | None = None,
        group_id: str | None = None,
        trace_metadata: dict[str, Any] | None = None,
        cancel_signal: asyncio.Event | None = None,
        deadline_seconds: float | None = None,
        _capture: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream events as the run progresses.

        ``user_input`` accepts the convenience shapes:
          - ``str``: wrapped as a single user-role ``Message``
          - ``Message``: appended verbatim
          - ``list[Message]``: appended verbatim (lets callers replay
            partial conversations or seed multi-turn context)

        The agent's ``instructions`` are prepended as a system message
        if no system/developer message is already present in the seed.

        Correlation kwargs (``parent_run_id`` / ``group_id`` /
        ``trace_metadata``) flow through ``ActionContext.metadata`` so
        middleware (TraceMiddleware in particular) can pin spans to a
        common parent. Ignore these for one-off runs.

        Cancellation: ``cancel_signal`` (cooperative) or
        ``deadline_seconds`` (wall-clock). When either fires, the
        generator yields a ``RunErrorEvent`` and stops. ``Runner.run()``
        re-raises ``RunCancelled``; raw ``stream()`` consumers see the
        error event without exception.

        ``_capture`` is internal — ``Runner.run()`` uses it to harvest
        the final ``RunResult``. External callers should ignore it.
        """

        run_id = run_id or f"run_{uuid.uuid4().hex[:16]}"
        seed_messages = self._normalize_input(user_input)
        history = self._with_system_prompt(agent, seed_messages)
        items: list[RunItem] = []
        usage = Usage()
        active_agent = agent
        turns = 0

        base_metadata: dict[str, Any] = dict(trace_metadata or {})
        if parent_run_id is not None:
            base_metadata["_parent_run_id"] = parent_run_id
        if group_id is not None:
            base_metadata["_group_id"] = group_id

        deadline_at = (
            time.monotonic() + deadline_seconds if deadline_seconds is not None else None
        )

        try:
            while turns < active_agent.max_turns:
                self._check_cancellation(cancel_signal, deadline_at, run_id, turns)
                turns += 1

                # ── LLM turn ─────────────────────────────────────────
                response = await self._call_llm(active_agent, history)
                usage.add(response.usage)
                assistant_msg = response.message
                history.append(assistant_msg)
                items.append(
                    MessageOutputItem(message=assistant_msg, usage=response.usage)
                )
                yield MessageDeltaEvent(
                    message=assistant_msg, turn_index=turns - 1
                )
                # Snapshot the cumulative usage — `usage` is mutable and
                # would otherwise reflect the final state by the time
                # the consumer reads earlier events.
                yield UsageUpdateEvent(
                    delta=response.usage, cumulative=_snapshot_usage(usage)
                )

                # ── Final output? ────────────────────────────────────
                if not assistant_msg.tool_calls:
                    final_text = self._extract_text(assistant_msg)
                    parsed = (
                        _validate_structured_output(active_agent, final_text)
                        if active_agent.output_type is not None
                        else None
                    )
                    result = RunResult(
                        run_id=run_id,
                        final_output=final_text,
                        final_message=assistant_msg,
                        history=history,
                        items=items,
                        usage=usage,
                        final_agent_name=active_agent.name,
                        turns=turns,
                        parent_run_id=parent_run_id,
                        group_id=group_id,
                        trace_metadata=dict(trace_metadata or {}),
                        parsed_output=parsed,
                    )
                    if _capture is not None:
                        _capture["result"] = result
                    yield RunCompletedEvent(
                        final_output=final_text,
                        final_agent_name=active_agent.name,
                        turns=turns,
                        usage=usage,
                        parsed_output=parsed,
                    )
                    return

                # ── Tool dispatches ──────────────────────────────────
                handed_off: Agent | None = None
                for call in assistant_msg.tool_calls:
                    self._check_cancellation(cancel_signal, deadline_at, run_id, turns)
                    items.append(ToolCallItem(call=call))
                    yield ToolCallStartedEvent(call=call, agent_name=active_agent.name)

                    result_item, action_result = await self._dispatch_tool(
                        active_agent, call, history, run_id, base_metadata
                    )
                    items.append(result_item)
                    yield ToolCallFinishedEvent(
                        result=result_item.result,
                        elapsed_seconds=result_item.elapsed_seconds,
                    )

                    history.append(
                        Message.tool(
                            call_id=call.id,
                            name=call.name,
                            content=action_result.output_as_string(),
                        )
                    )

                    if action_result.handoff_target is not None:
                        items.append(
                            HandoffItem(
                                from_agent=active_agent.name,
                                to_agent=action_result.handoff_target.name,
                                reason=action_result.metadata.get("handoff_reason"),
                            )
                        )
                        yield HandoffEvent(
                            from_agent=active_agent.name,
                            to_agent=action_result.handoff_target.name,
                            reason=action_result.metadata.get("handoff_reason"),
                        )
                        handed_off = action_result.handoff_target
                        break

                if handed_off is not None:
                    active_agent = handed_off

            # Loop exhausted without a final-output return → max turns.
            err = MaxTurnsExceeded(
                f"Run {run_id} exceeded max_turns={active_agent.max_turns} "
                f"on agent {active_agent.name!r}"
            )
            if _capture is not None:
                _capture["error"] = err
            yield RunErrorEvent(
                error_type="MaxTurnsExceeded",
                message=str(err),
                turns_completed=turns,
            )
        except RunCancelled as exc:
            if _capture is not None:
                _capture["error"] = exc
            yield RunErrorEvent(
                error_type="RunCancelled",
                message=str(exc),
                turns_completed=exc.turns_completed,
                metadata={"reason": exc.reason},
            )
        except (OutputValidationError, ModelBehaviorError, UserError) as exc:
            if _capture is not None:
                _capture["error"] = exc
            yield RunErrorEvent(
                error_type=type(exc).__name__,
                message=str(exc),
                turns_completed=turns,
            )

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _check_cancellation(
        cancel_signal: asyncio.Event | None,
        deadline_at: float | None,
        run_id: str,
        turns: int,
    ) -> None:
        """Raise ``RunCancelled`` if either condition is now true.

        Called at each safe yield point: top of the main loop and
        before every tool dispatch. NOT called inside the LLM /
        handler awaits themselves — those call sites are owned by
        the provider / handler author and may have their own timeout
        semantics.
        """
        if cancel_signal is not None and cancel_signal.is_set():
            raise RunCancelled("signal", run_id, turns)
        if deadline_at is not None and time.monotonic() >= deadline_at:
            raise RunCancelled("deadline", run_id, turns)

    @staticmethod
    def _normalize_input(
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
                        f"Runner.run user_input list must contain Message, "
                        f"got {type(msg).__name__}"
                    )
            return list(user_input)
        raise UserError(
            f"Runner.run user_input must be str | Message | list[Message], "
            f"got {type(user_input).__name__}"
        )

    @staticmethod
    def _with_system_prompt(agent: Agent, seed: list[Message]) -> list[Message]:
        """Prepend the agent's system prompt unless caller already supplied one."""
        if any(m.role in (Role.SYSTEM, Role.DEVELOPER) for m in seed):
            return list(seed)
        prompt = agent.system_message()
        if not prompt:
            return list(seed)
        return [Message.system(prompt), *seed]

    @staticmethod
    async def _call_llm(
        agent: Agent, history: list[Message]
    ) -> ChatCompletionResponse:
        """Invoke the provider and validate the result shape."""
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
        if response.finish_reason is FinishReason.TOOL_CALLS and not response.message.tool_calls:
            raise ModelBehaviorError(
                "Provider reported finish_reason=tool_calls but emitted no tool_calls"
            )
        return response

    async def _dispatch_tool(
        self,
        agent: Agent,
        call: ToolCall,
        history: list[Message],
        run_id: str,
        base_metadata: dict[str, Any],
    ) -> tuple[ToolResultItem, ActionResult]:
        """Look up the Action for ``call``, validate input, compose the
        middleware chain, run the handler, capture timing.

        Middleware composition order (outermost → innermost):
          run-level middleware → action-level middleware → handler
        """
        action = agent.registry.get(call.name)
        if action is None:
            err = ActionResult(
                error=f"Unknown tool {call.name!r}. "
                f"Available: {', '.join(sorted(a.name for a in agent.registry))}"
            )
            return (
                ToolResultItem(
                    result=ToolResult(
                        call_id=call.id, name=call.name, error=err.error
                    ),
                    elapsed_seconds=0.0,
                ),
                err,
            )

        if action.kind is ActionKind.BG_TASK:
            # Spawn via BgTaskRunner; return handle immediately so the
            # LLM can move on. Subsequent check_bg_task / cancel_bg_task
            # tool calls inspect / control via the same runner.
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
            bg_ctx = ActionContext(
                run_id=run_id,
                agent_name=agent.name,
                params=params,
                history=tuple(history),
                metadata=dict(base_metadata),
            )
            handle = self.bg_task_runner.spawn(action, bg_ctx)
            output = handle.to_summary()
            wire_result = ToolResult(
                call_id=call.id,
                name=call.name,
                output=str(output),
            )
            return (
                ToolResultItem(result=wire_result, elapsed_seconds=0.0),
                ActionResult(output=output),
            )

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

        # Schema-driven input validation. Failures become recoverable
        # tool errors (LLM sees ``[input-validation] missing required
        # field 'query'`` and can retry with corrected args) rather
        # than crashing the run.
        validation_error = _validate_tool_input(action.input_schema, params)
        if validation_error is not None:
            err = ActionResult(error=f"[input-validation] {validation_error}")
            return (
                ToolResultItem(
                    result=ToolResult(
                        call_id=call.id, name=call.name, error=err.error
                    ),
                    elapsed_seconds=0.0,
                ),
                err,
            )

        ctx = ActionContext(
            run_id=run_id,
            agent_name=agent.name,
            params=params,
            history=tuple(history),
            metadata=dict(base_metadata),
        )

        chain = list(self._middleware) + list(action.middleware)
        wrapped_handler = compose_chain(action, chain)

        started = time.perf_counter()
        try:
            handler_return = wrapped_handler(ctx)
            if not inspect.isawaitable(handler_return):
                raise UserError(
                    f"Action {action.name!r} handler did not return an awaitable; "
                    "handlers (and middleware) must be async."
                )
            result = await handler_return
        except Exception as exc:  # noqa: BLE001
            elapsed = time.perf_counter() - started
            err = ActionResult(error=f"{type(exc).__name__}: {exc}")
            return (
                ToolResultItem(
                    result=ToolResult(
                        call_id=call.id, name=call.name, error=err.error
                    ),
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

    @staticmethod
    def _extract_text(message: Message) -> str:
        """Pull the assistant's text out of either string or content-parts shape."""
        if isinstance(message.content, str):
            return message.content
        parts: list[str] = []
        for part in message.content:
            text = getattr(part, "text", None)
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)


# ── Tool input validation ─────────────────────────────────────────────


_JSON_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
    "null": (type(None),),
}


def _validate_tool_input(
    schema: dict[str, Any] | None, params: dict[str, Any]
) -> str | None:
    """Lightweight JSON-Schema-subset validator for tool args.

    Returns ``None`` on success, or a human-readable error string
    suitable for forwarding to the LLM.

    Covers the cases LLMs actually fail on:
      - missing ``required`` fields
      - wrong primitive type at top level (string / number / integer /
        boolean / array / object / null)
      - extra fields when ``additionalProperties: false`` is set
      - integer when a number is allowed (booleans rejected for
        numeric slots — Python bools are int subclasses but the LLM
        rarely actually means True for a "limit" field)

    Deliberately NOT covered (Sprint 3 may swap in jsonschema if real
    deployments need it):
      - Nested object validation
      - String formats / patterns / minLength / maxLength
      - Number ranges (minimum / maximum)
      - Enum validation
      - oneOf / anyOf / allOf
    """
    if not schema or not isinstance(schema, dict):
        return None

    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        properties = {}

    required = schema.get("required", [])
    if isinstance(required, list):
        for field_name in required:
            if field_name not in params:
                return f"missing required field {field_name!r}"

    additional = schema.get("additionalProperties", True)
    if additional is False:
        for key in params:
            if key not in properties:
                return f"unexpected field {key!r} (additionalProperties: false)"

    for key, value in params.items():
        prop_schema = properties.get(key)
        if not isinstance(prop_schema, dict):
            continue
        prop_type = prop_schema.get("type")
        if prop_type is None:
            continue
        # Accept either a single type string or a list of allowed types.
        allowed_types: list[str] = (
            [prop_type] if isinstance(prop_type, str) else list(prop_type)
        )
        ok = False
        for allowed in allowed_types:
            py_types = _JSON_TYPE_MAP.get(allowed)
            if py_types is None:
                ok = True  # unknown declared type → trust the LLM
                break
            # Reject booleans for numeric slots even though bool is int.
            if allowed in ("number", "integer") and isinstance(value, bool):
                continue
            if isinstance(value, py_types):
                ok = True
                break
        if not ok:
            return (
                f"field {key!r} has wrong type: expected {prop_type!r}, "
                f"got {type(value).__name__}"
            )

    return None


# ── Structured output validation ──────────────────────────────────────


def _validate_structured_output(agent: Agent, text: str) -> Any:
    """Parse + validate the assistant's final text against ``agent.output_type``.

    Strategy:
      1. Strip a markdown JSON fence if present (LLMs love ```json ... ```)
      2. ``json.loads`` the payload
      3. If output_type is a Pydantic model, ``model_validate``; else
         coerce via ``output_type(**payload)`` for plain dataclasses
         and similar callable shapes.

    Raises ``OutputValidationError`` on any failure. Callers wanting
    automatic retry should wrap their own loop around ``Runner.run()``;
    Sprint 3's StateMachine will introduce a REFLECTING phase that can
    drive structured-output retries inside the runner itself.
    """
    payload = _strip_json_fence(text)
    try:
        decoded = json.loads(payload) if payload else None
    except json.JSONDecodeError as exc:
        raise OutputValidationError(text, f"JSON decode failed: {exc}") from exc

    output_type = agent.output_type
    if output_type is None:
        return decoded

    # Pydantic v2 model
    model_validate = getattr(output_type, "model_validate", None)
    if callable(model_validate):
        try:
            return model_validate(decoded)
        except Exception as exc:  # noqa: BLE001
            raise OutputValidationError(text, str(exc)) from exc

    # Pydantic v1 fallback
    parse_obj = getattr(output_type, "parse_obj", None)
    if callable(parse_obj):
        try:
            return parse_obj(decoded)
        except Exception as exc:  # noqa: BLE001
            raise OutputValidationError(text, str(exc)) from exc

    # Plain dataclass / TypedDict / callable: try to instantiate
    try:
        if isinstance(decoded, dict):
            return output_type(**decoded)
        return output_type(decoded)
    except Exception as exc:  # noqa: BLE001
        raise OutputValidationError(text, str(exc)) from exc


def _snapshot_usage(u: Usage) -> Usage:
    """Shallow copy of ``Usage`` so a streaming consumer sees the
    cumulative-as-of-this-event value rather than the live (mutable)
    aggregate that keeps growing during the run."""
    snap = Usage(
        requests=u.requests,
        input_tokens=u.input_tokens,
        output_tokens=u.output_tokens,
        total_tokens=u.total_tokens,
    )
    # Preserve detail breakdowns and entries — the dataclass init copies
    # field defaults, so we have to re-assign after construction.
    snap.input_tokens_details = type(u.input_tokens_details)(
        cached_tokens=u.input_tokens_details.cached_tokens
    )
    snap.output_tokens_details = type(u.output_tokens_details)(
        reasoning_tokens=u.output_tokens_details.reasoning_tokens
    )
    snap.request_usage_entries = list(u.request_usage_entries)
    return snap


_FENCE_RE = None  # lazily compiled


def _strip_json_fence(text: str) -> str:
    """Strip ```json ... ``` markdown fencing from a string.

    Idempotent: returns ``text`` unchanged if no fence detected.
    Tolerates leading whitespace, optional language tag, trailing
    whitespace.
    """
    import re

    global _FENCE_RE
    if _FENCE_RE is None:
        _FENCE_RE = re.compile(
            r"^\s*```(?:json|JSON)?\s*\n?(.*?)\n?\s*```\s*$",
            re.DOTALL,
        )
    match = _FENCE_RE.match(text)
    return match.group(1) if match else text


# ── Public surface ───────────────────────────────────────────────────────


__all__ = ["Runner", "RunResult"]
