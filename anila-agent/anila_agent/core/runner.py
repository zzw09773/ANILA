"""AnilaRunner — wraps `agents.Runner` with hook firing, abort handling, and an event stream.

The runner is intentionally thin. The agent loop, model calls, and tool dispatch all live
in openai-agents; AnilaRunner adds:

  - Pre/post/stop hook firing via `AnilaRunHooks`.
  - `SessionStart` / `UserPromptSubmit` events at the boundaries.
  - A typed `RunSummary` so callers do not need to dig through openai-agents internals.
"""

from __future__ import annotations

import asyncio
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

        return RunSummary(
            final_output=result.final_output,
            turns_used=hooks.turns,
            aborted=False,
            abort_reason=None,
        )

    def send_sync(self, prompt: str) -> RunSummary:
        return asyncio.run(self.send(prompt))
