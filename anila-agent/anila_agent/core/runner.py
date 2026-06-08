"""AnilaRunner — wraps `agents.Runner` with hook firing, abort handling, and an event stream.

The runner is intentionally thin. The agent loop, model calls, and tool dispatch all live
in openai-agents; AnilaRunner adds:

  - Pre/post/stop hook firing via `AnilaRunHooks`.
  - `SessionStart` / `UserPromptSubmit` events at the boundaries.
  - A typed `RunSummary` so callers do not need to dig through openai-agents internals.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from agents import Runner

from anila_agent.core.agent import AssembledAgent
from anila_agent.core.hooks import (
    AnilaRunHooks,
    fire_session_start,
    fire_user_prompt_submit,
)
from anila_agent.utils.logging import get_logger

logger = get_logger(__name__)


# gemma's native tool-call tokens. Captured live from gemma-4-31B-it (vLLM):
#   <|tool_call>call:get_weather{city: '台北'}<tool_call|>
# Note the ASYMMETRIC delimiters (``<|tool_call>`` open, ``<tool_call|>`` close)
# and that several blocks can be emitted back-to-back. When the serving stack's
# tool-call parser misses this (observed intermittently), the raw block lands in
# the assistant content / final_output instead of being executed as a tool call.
# Match up to the close token OR end-of-string (the block can be truncated by
# max_tokens). DOTALL so multi-line arg payloads are covered.
_LEAKED_TOOL_CALL_RE = re.compile(r"<\|tool_call>.*?(?:<tool_call\|>|$)", re.DOTALL)


def _strip_leaked_tool_calls(text: str) -> tuple[str, int]:
    """Strip gemma-native tool-call tokens that leaked into model content.

    Returns ``(cleaned_text, num_blocks_removed)``. When blocks were removed the
    result is whitespace-stripped so a content that was *only* a leaked call
    collapses to "" rather than leaving stray newlines.
    """
    cleaned, n = _LEAKED_TOOL_CALL_RE.subn("", text)
    if n:
        cleaned = cleaned.strip()
    return cleaned, n


@dataclass(frozen=True)
class RunSummary:
    """Structured result returned from `AnilaRunner.run`."""

    final_output: Any
    turns_used: int
    aborted: bool
    abort_reason: str | None


class AnilaRunner:
    """Stateful entry point. One instance per session.

    Typical usage:

        config = load_config()
        assembled = build_agent(config, session_id="alice")
        runner = AnilaRunner(assembled)
        await runner.start()
        summary = await runner.send("hello")
    """

    def __init__(self, assembled: AssembledAgent, *, session_id: str = "default") -> None:
        self.assembled = assembled
        self.session_id = session_id
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        await fire_session_start(
            self.assembled.hook_registry,
            self.assembled.event_bus,
            session_id=self.session_id,
            agent_name=self.assembled.agent.name,
        )
        self._started = True

    async def send(self, prompt: str) -> RunSummary:
        if not self._started:
            await self.start()

        await fire_user_prompt_submit(
            self.assembled.hook_registry,
            self.assembled.event_bus,
            prompt=prompt,
            session_id=self.session_id,
        )

        hooks = AnilaRunHooks(
            self.assembled.hook_registry,
            self.assembled.event_bus,
            agent_name=self.assembled.agent.name,
        )

        try:
            result = await Runner.run(
                starting_agent=self.assembled.agent,
                input=prompt,
                hooks=hooks,
                session=self.assembled.short_term,
                max_turns=self.assembled.max_turns,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("agent run failed")
            self.assembled.event_bus.emit("error", message=str(e))
            return RunSummary(
                final_output=None,
                turns_used=hooks.turns,
                aborted=True,
                abort_reason=str(e),
            )

        final_output = result.final_output
        if isinstance(final_output, str):
            final_output, leaked = _strip_leaked_tool_calls(final_output)
            if leaked:
                logger.warning(
                    "stripped %d leaked tool-call token block(s) from "
                    "final_output — the model emitted gemma-native "
                    "<|tool_call> tokens that the harness did not parse as a "
                    "tool call (check the serving tool-call parser config)",
                    leaked,
                )

        return RunSummary(
            final_output=final_output,
            turns_used=hooks.turns,
            aborted=False,
            abort_reason=None,
        )

    def send_sync(self, prompt: str) -> RunSummary:
        return asyncio.run(self.send(prompt))
