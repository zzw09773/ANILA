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
from .budget_tracker import BudgetTracker, ContinueDecision, check_token_budget

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
    ) -> None:
        self._provider = provider
        self._tools = tool_registry
        self._config = config
        self._session_id = session_id
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
                results = await execute_batch(
                    self._tools,
                    assistant_msg.tool_calls,
                )
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
