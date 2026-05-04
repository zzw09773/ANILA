"""QueryEngine - 7-stage turn loop for the ANILA Core agent runtime.

Stages per turn:
  1. pre_process   - inject memory, compact check, budget message
  2. api_call      - stream from provider
  3. completion_check - check finish_reason
  4. tool_execution - execute tool calls via tool router
  5. attachments   - attach tool results back to history
  6. limit_check   - max_turns, budget, token limit
  7. continue_or_stop - budget tracker decision

Post-turn hooks (non-blocking, fire-and-forget):
  - memory_extraction_hook
  - session_memory_hook
  - auto_dream_check_hook
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Optional

from ..memory.short_term import Session
from ..models.message import (
    AssistantMessage,
    Message,
    ToolCall,
    ToolResult,
    Usage,
    UserMessage,
)
from ..config import settings
from ..providers.base import Provider, ProviderRequest
from ..router.tool_router import ToolRegistry, execute_batch
from .approvals import (
    MultipleInterruptsError,
    RunPaused,
    resume_tool_approval,
    resume_with,
    to_record,
)
from .budget_tracker import BudgetTracker, ContinueDecision, check_token_budget
from .handoff import RunHandoff
from .lifecycle import RunHooks, _safe_call

logger = logging.getLogger(__name__)

PostTurnHook = Callable[["TurnResult"], Coroutine[Any, Any, None]]


@dataclass
class QueryConfig:
    """Configuration for a single query execution."""

    max_turns: int = 10
    budget_tokens: Optional[int] = None
    agent_id: Optional[str] = None
    system_prompt: str = ""
    model: str = settings.model  # 從 .env MODEL= 讀取
    temperature: float = 0.0
    max_tokens: int = 4096
    context_window: int = 200_000
    tool_names: Optional[list[str]] = None  # None = all registered tools


@dataclass
class TurnResult:
    """Result of a completed turn loop."""

    messages: list[Message]
    total_usage: Usage
    turn_count: int
    finish_reason: str
    was_compacted: bool = False
    stop_reason: str = "max_turns"


class QueryEngine:
    """Stateful engine that runs the 7-stage turn loop.

    Designed to be instantiated per-request (not a singleton).
    All state is instance-level, not module-level.
    """

    def __init__(
        self,
        provider: Provider,
        tool_registry: ToolRegistry,
        config: QueryConfig,
        session_id: str = "",
        session: Optional[Session] = None,
        hooks: Optional[RunHooks] = None,
    ) -> None:
        self._provider = provider
        self._tools = tool_registry
        self._config = config
        # ``session_id`` (string) predates the Session abstraction and is kept
        # for back-compat; ``session`` (Sprint 9) is what enables pause-resume
        # and conversation persistence. When both are passed we trust the
        # Session's own session_id field.
        self._session_id = session.session_id if session else session_id
        self._session = session
        # Sprint 11 PR 1: synchronous lifecycle hooks. None = no-op.
        self._hooks = hooks
        self._post_turn_hooks: list[PostTurnHook] = []
        self._budget_tracker = BudgetTracker()
        self._in_flight_hooks: set[asyncio.Task] = set()  # type: ignore[type-arg]

    def add_post_turn_hook(self, hook: PostTurnHook) -> None:
        """Register a non-blocking post-turn hook."""
        self._post_turn_hooks.append(hook)

    async def run(
        self,
        messages: list[Message],
        on_stream_delta: Optional[Callable[[Any], Coroutine[Any, Any, None]]] = None,
    ) -> TurnResult:
        """Execute the turn loop until completion or limit.

        Args:
            messages: Initial conversation history (not mutated).
            on_stream_delta: Optional async callback for each stream event.

        Returns:
            TurnResult with final message list and usage totals.
        """
        # Work on an independent copy - immutable pattern
        history = list(messages)
        total_usage = Usage()
        turn_count = 0
        stop_reason = "completed"

        # Sprint 11 PR 1: lifecycle hook firing point. Fires once per run()
        # entry; on_agent_start fires once per agent activation (which for
        # the current single-agent QueryEngine is also once per run).
        agent_id = self._config.agent_id or ""
        await _safe_call(
            self._hooks, "on_run_start",
            agent_id=agent_id, session_id=self._session_id,
        )
        await _safe_call(
            self._hooks, "on_agent_start",
            agent_id=agent_id, session_id=self._session_id,
        )

        while turn_count < self._config.max_turns:
            turn_count += 1

            # Stage 1: pre_process
            history, budget_message = await self._pre_process(history)

            # Stage 2: api_call
            assistant_msg, usage, finish_reason = await self._api_call(
                history,
                on_stream_delta=on_stream_delta,
            )
            history = history + [assistant_msg]
            total_usage = total_usage.add(usage)

            # Stage 3: completion_check
            if finish_reason in {"end_turn", "stop", "length"}:
                if not assistant_msg.has_tool_calls():
                    # Model produced a final answer
                    stop_reason = "completed"
                    break

            # Stage 4: tool_execution
            if assistant_msg.has_tool_calls():
                # Sprint 11 PR 1: per-tool start hooks fire BEFORE batch
                # execution so an instrumentation layer can stamp
                # in-flight spans / stash request payloads. Hooks are
                # always-await and never abort the dispatch.
                for tc in assistant_msg.tool_calls:
                    await _safe_call(
                        self._hooks, "on_tool_start",
                        agent_id=agent_id,
                        session_id=self._session_id,
                        call=tc,
                    )

                results = await execute_batch(
                    self._tools,
                    assistant_msg.tool_calls,
                )

                # Pair each result back to its call by tool_call_id so
                # on_tool_end carries a matched (call, result) pair.
                calls_by_id = {tc.id: tc for tc in assistant_msg.tool_calls}
                for r in results:
                    matched_call = calls_by_id.get(r.tool_call_id)
                    if matched_call is not None:
                        await _safe_call(
                            self._hooks, "on_tool_end",
                            agent_id=agent_id,
                            session_id=self._session_id,
                            call=matched_call,
                            result=r,
                        )

                # Approvals: when a tool returned an InterruptItem, persist
                # conversation state + the interrupt to Session and raise
                # RunPaused so the SSE handler can flush its stream cleanly.
                interrupted = [r for r in results if r.interrupt is not None]
                if interrupted:
                    await self._pause_on_interrupt(
                        history=history,
                        results=results,
                        assistant_msg=assistant_msg,
                    )
                    # _pause_on_interrupt always raises; the explicit raise
                    # below is unreachable but keeps mypy happy about flow.
                    raise AssertionError("_pause_on_interrupt must raise")

                # Handoff (Sprint 10 PR 1): control transfer to another
                # agent. Persist current conversation, then raise
                # RunHandoff carrying the request — the Router catches it
                # and dispatches the target agent with filtered context.
                handoffs = [r for r in results if r.handoff is not None]
                if handoffs:
                    await self._handoff(
                        history=history,
                        handoff_result=handoffs[0],
                    )
                    raise AssertionError("_handoff must raise")

                # Stage 5: attachments
                tool_user_msg = self._build_tool_result_message(results)
                history = history + [tool_user_msg]

            # Stage 6: limit_check
            if turn_count >= self._config.max_turns:
                stop_reason = "max_turns"
                break

            # Stage 7: continue_or_stop via budget tracker
            # Only apply budget pressure when a budget is explicitly set
            if self._config.budget_tokens:
                output_tokens = total_usage.output_tokens
                decision = check_token_budget(
                    self._budget_tracker,
                    self._config.agent_id,
                    self._config.budget_tokens,
                    output_tokens,
                )
                if decision.action == "stop" and getattr(decision, "has_event", False):
                    stop_reason = "budget"
                    break

                # Inject budget nudge message if continuing
                if isinstance(decision, ContinueDecision) and decision.nudge_message:
                    nudge = UserMessage(
                        content=[{"type": "text", "text": decision.nudge_message}]
                    )
                    history = history + [nudge]

        result = TurnResult(
            messages=history,
            total_usage=total_usage,
            turn_count=turn_count,
            finish_reason=stop_reason,
            stop_reason=stop_reason,
        )

        # Sprint 11 PR 1: synchronous lifecycle close-out — fires before
        # the fire-and-forget post_turn_hooks so observers see the run
        # end before any async work spawned by them races them.
        await _safe_call(
            self._hooks, "on_agent_end",
            agent_id=agent_id, session_id=self._session_id, result=result,
        )
        await _safe_call(
            self._hooks, "on_run_end",
            agent_id=agent_id, session_id=self._session_id, result=result,
        )

        # Fire post-turn hooks non-blocking
        await self._fire_post_turn_hooks(result)

        return result

    async def _pre_process(
        self, history: list[Message]
    ) -> tuple[list[Message], Optional[str]]:
        """Stage 1: prepare the history for the upcoming API call.

        Sprint 1 boundary cleanup removed the RagPreprocessor injection
        path — the new model is tool-driven (the LLM decides when to
        search via registered tools). This stage is now a passthrough,
        kept as a hook for future preprocessing concerns (token budget
        gates, redaction, etc).
        """
        return history, None

    async def _api_call(
        self,
        history: list[Message],
        on_stream_delta: Optional[Callable] = None,
    ) -> tuple[AssistantMessage, Usage, str]:
        """Stage 2: call the provider and collect the response."""
        tool_schemas = self._tools.openai_schemas(self._config.tool_names)

        request = ProviderRequest(
            model=self._config.model,
            system=self._config.system_prompt,
            messages=history,
            tools=tool_schemas,
            max_tokens=self._config.max_tokens,
            temperature=self._config.temperature,
        )

        content_parts: list[dict] = []
        tool_calls: list[ToolCall] = []
        usage = Usage()
        finish_reason = "end_turn"

        partial_tool_calls: dict[str, dict] = {}

        async for delta in self._provider.stream_completion(request):
            if on_stream_delta:
                await on_stream_delta(delta)

            if delta.type == "text" and delta.text:
                content_parts.append({"type": "text", "text": delta.text})

            elif delta.type == "tool_call" and delta.tool_call:
                tc = delta.tool_call
                if tc.id not in partial_tool_calls:
                    partial_tool_calls[tc.id] = {
                        "id": tc.id,
                        "name": tc.name,
                        "input_raw": "",
                    }
                partial_tool_calls[tc.id]["input_raw"] += tc.input_partial

            elif delta.type == "stop":
                if delta.finish_reason:
                    finish_reason = delta.finish_reason
                if delta.usage:
                    usage = delta.usage

        # Resolve tool calls from accumulated partial inputs
        for tc_data in partial_tool_calls.values():
            try:
                import json
                parsed_input = json.loads(tc_data["input_raw"]) if tc_data["input_raw"] else {}
            except (ValueError, KeyError):
                parsed_input = {}
            tool_calls.append(
                ToolCall(id=tc_data["id"], name=tc_data["name"], input=parsed_input)
            )

        if not content_parts and not tool_calls:
            content_parts.append({"type": "text", "text": ""})

        assistant_msg = AssistantMessage(
            content=content_parts if content_parts else "",
            tool_calls=tool_calls,
            usage=usage,
        )
        return assistant_msg, usage, finish_reason

    def _build_tool_result_message(self, results: list[ToolResult]) -> UserMessage:
        """Stage 5: wrap tool results into a user message."""
        content_blocks: list[dict] = []
        for result in results:
            block: dict = {
                "type": "tool_result",
                "tool_use_id": result.tool_call_id,
                "content": result.content if isinstance(result.content, str)
                           else result.as_text(),
            }
            if result.is_error:
                block["is_error"] = True
            content_blocks.append(block)
        return UserMessage(content=content_blocks)

    async def _handoff(
        self,
        *,
        history: list[Message],
        handoff_result: ToolResult,
    ) -> None:
        """Persist conversation, then raise :class:`RunHandoff`.

        Unlike interrupts (which the user must answer), a handoff is a
        control transfer to another agent — the Router catches the
        exception and dispatches the target with filtered context.
        Persistence still happens so callers that re-create the engine
        can rehydrate the source agent's session if needed.
        """
        request = handoff_result.handoff
        assert request is not None  # by construction in caller
        if self._session is not None:
            await self._session.add_items(history)
        # No interrupt record is pushed — the Router consumes the request
        # immediately. Sprint 10 PR 3 may revisit this if we want resume
        # semantics for handoffs.
        await _safe_call(
            self._hooks, "on_handoff",
            source_agent_id=self._config.agent_id or "",
            session_id=self._session.session_id if self._session else "",
            request=request,
        )
        raise RunHandoff(
            session_id=self._session.session_id if self._session else "",
            request=request,
        )

    async def _pause_on_interrupt(
        self,
        *,
        history: list[Message],
        results: list[ToolResult],
        assistant_msg: AssistantMessage,
    ) -> None:
        """Persist conversation + interrupt to Session, then raise RunPaused.

        ``history`` already includes ``assistant_msg`` (Stage 2 appended it).
        We persist the full history so that ``resume_from_interrupt`` can
        rehydrate without depending on the caller to keep state.
        """
        if self._session is None:
            raise RuntimeError(
                "Tool returned InterruptItem but QueryEngine has no Session. "
                "Pass session=… to QueryEngine to enable pause-resume."
            )
        interrupted = [r for r in results if r.interrupt is not None]
        if len(interrupted) > 1:
            raise MultipleInterruptsError(
                f"{len(interrupted)} tools returned InterruptItem in one turn; "
                "Sprint 9 supports at most one. Tools: "
                + ", ".join(r.tool_call_id for r in interrupted)
            )
        interrupted_result = interrupted[0]
        # mypy: interrupted_result.interrupt is not None by construction.
        assert interrupted_result.interrupt is not None
        sibling_results = [r for r in results if r.interrupt is None]
        interrupted_call = next(
            c for c in assistant_msg.tool_calls
            if c.id == interrupted_result.tool_call_id
        )

        # Persist *full* history (including assistant_msg) before raising;
        # resume_from_interrupt rehydrates from this snapshot.
        await self._session.add_items(history)
        record = to_record(
            interrupted_result.interrupt,
            tool_call=interrupted_call,
            sibling_results=sibling_results,
        )
        await self._session.push_interrupt(record)
        await _safe_call(
            self._hooks, "on_run_paused",
            agent_id=self._config.agent_id or "",
            session_id=self._session.session_id,
            interrupt_id=record.id,
            kind=record.kind,
        )
        raise RunPaused(
            session_id=self._session.session_id,
            interrupt_id=record.id,
            kind=record.kind,  # type: ignore[arg-type]
        )

    async def resume_from_interrupt(
        self,
        interrupt_id: str,
        answer: "dict[str, Any] | str",
        on_stream_delta: Optional[
            Callable[[Any], Coroutine[Any, Any, None]]
        ] = None,
    ) -> TurnResult:
        """Resume a paused run with the user's answer.

        Loads conversation history from session, pops the named interrupt,
        builds the resume :class:`UserMessage` (with sibling tool results
        + the user's answer), then re-enters :meth:`run` from there.

        Raises:
            RuntimeError: if no Session is configured.
            ValueError: if the interrupt_id is unknown / already answered.
        """
        if self._session is None:
            raise RuntimeError(
                "resume_from_interrupt requires a Session. "
                "Pass session=… to QueryEngine."
            )
        history = await self._session.get_items()
        # Sprint 11 PR 3: ``tool_approval`` interrupts have a different
        # resume shape — the source tool runs (or doesn't) instead of
        # the user's answer being treated as the tool's reply text.
        # Peek at the pending interrupt to decide which helper to use.
        pending = await self._session.pending_interrupts()
        target = next((p for p in pending if p.id == interrupt_id), None)
        if target is not None and target.kind == "tool_approval":
            if isinstance(answer, dict):
                approved = bool(answer.get("approved", False))
                comment = str(answer.get("comment", "") or "")
            else:
                # Permissive fallback: a string answer "yes" / "approve"
                # counts as approval; anything else is a deny.
                approved = str(answer).strip().lower() in {
                    "yes", "approve", "approved", "true", "ok"
                }
                comment = ""
            resume_msg = await resume_tool_approval(
                self._session, self._tools, interrupt_id,
                approved=approved, comment=comment,
            )
        else:
            resume_msg = await resume_with(
                self._session, interrupt_id, answer
            )
        await _safe_call(
            self._hooks, "on_run_resumed",
            agent_id=self._config.agent_id or "",
            session_id=self._session.session_id,
            interrupt_id=interrupt_id,
        )
        # Resume by re-calling run() with the hydrated + completed history.
        # The next turn starts at Stage 1 with the full picture and the
        # model sees the user's answer as the awaited tool_result block.
        return await self.run(history + [resume_msg], on_stream_delta=on_stream_delta)

    async def _fire_post_turn_hooks(self, result: TurnResult) -> None:
        """Launch post-turn hooks as fire-and-forget async tasks."""
        for hook in self._post_turn_hooks:
            task = asyncio.create_task(self._safe_hook(hook, result))
            self._in_flight_hooks.add(task)
            task.add_done_callback(self._in_flight_hooks.discard)

    async def _safe_hook(self, hook: PostTurnHook, result: TurnResult) -> None:
        """Run a hook, swallowing any exception to protect the main loop."""
        try:
            await hook(result)
        except Exception as exc:
            logger.warning("Post-turn hook %r raised: %s", hook, exc)

    async def drain_hooks(self, timeout: float = 30.0) -> None:
        """Wait for all in-flight post-turn hooks to complete."""
        if not self._in_flight_hooks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._in_flight_hooks, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("Post-turn hooks did not complete within %.1fs", timeout)
