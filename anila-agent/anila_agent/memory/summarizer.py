"""End-of-turn auto memory extractor. Disabled by default.

Wired into the runner as a Stop hook when `memory.yaml.auto_memory.enabled` is true.
The hook collects the recent turn's user message + final output, asks the model to
emit zero or more memory file proposals, and writes them via `MemdirStore`.

The proposal schema is intentionally narrow: name, description, type, body.
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from typing import Any

from agents import Agent, ModelSettings, Runner

from anila_agent.core.hooks import HookOutput, StopInput
from anila_agent.memory.long_term import LongTermMemory
from anila_agent.memory.store import MemdirStore
from anila_agent.utils.logging import get_logger

logger = get_logger(__name__)

_EXTRACT_SYSTEM_PROMPT = """You extract durable memories from a finished assistant turn.

Output strict JSON:
{
  "memories": [
    {
      "filename": "user_role.md",
      "name": "short title",
      "description": "one-line description used to decide relevance later",
      "type": "user|feedback|project|reference",
      "body": "free-form markdown describing the memory"
    }
  ]
}

Rules:
- Save at most 3 memories. Empty list is preferred when nothing is durably worth saving.
- DO NOT save: code patterns, file paths, git history, debugging recipes, ephemeral task state.
- DO save: user role/preferences, corrections, project decisions with WHY, external system pointers.
- Filename: lowercase snake_case ending in `.md`.
- For feedback/project, include "**Why:**" and "**How to apply:**" lines in the body.
"""


@dataclass
class _State:
    enabled: bool = False
    memory: LongTermMemory | None = None
    model: Any = None
    min_messages: int = 4
    last_run_turn: int = 0
    lock: threading.Lock = threading.Lock()


_STATE = _State()


def configure(
    *,
    memory: LongTermMemory | None,
    model: Any,
    enabled: bool,
    min_messages_between_runs: int = 4,
) -> None:
    """Initialise the summarizer. The runner calls this from `build_agent`."""
    _STATE.enabled = enabled
    _STATE.memory = memory
    _STATE.model = model
    _STATE.min_messages = max(1, int(min_messages_between_runs))
    _STATE.last_run_turn = 0


def is_enabled() -> bool:
    return _STATE.enabled and _STATE.memory is not None


async def extract_on_stop(payload: StopInput) -> HookOutput:
    """Stop hook entry point. Registered via tools.yaml when auto_memory is enabled."""
    if not is_enabled():
        return HookOutput()
    with _STATE.lock:
        if payload.turns_used - _STATE.last_run_turn < _STATE.min_messages:
            return HookOutput()
        _STATE.last_run_turn = payload.turns_used

    final_output = _coerce_text(payload.final_output)
    if not final_output:
        return HookOutput()

    try:
        proposals = await _propose(final_output)
    except Exception as e:  # noqa: BLE001
        logger.warning("auto memory extraction failed: %s", e)
        return HookOutput()

    if not proposals:
        return HookOutput()

    saved = _persist(proposals)
    if saved:
        logger.info("auto memory saved: %s", ", ".join(saved))
        return HookOutput(additional_context=f"Saved memories: {', '.join(saved)}")
    return HookOutput()


def extract_now(turn_text: str) -> list[str]:
    """Synchronous entry for tests / `/memory extract` command."""
    if not is_enabled():
        return []
    proposals = asyncio.run(_propose(turn_text))
    return _persist(proposals)


async def _propose(turn_text: str) -> list[dict[str, Any]]:
    if _STATE.model is None:
        return []
    agent = Agent[Any](
        name="anila-memory-extractor",
        instructions=_EXTRACT_SYSTEM_PROMPT,
        model=_STATE.model,
        model_settings=ModelSettings(temperature=0.0, max_tokens=1024),
    )
    result = await Runner.run(starting_agent=agent, input=turn_text, max_turns=1)
    text = (result.final_output or "").strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    items = parsed.get("memories") if isinstance(parsed, dict) else None
    return items if isinstance(items, list) else []


def _persist(proposals: list[dict[str, Any]]) -> list[str]:
    memory = _STATE.memory
    if memory is None:
        return []
    saved: list[str] = []
    for item in proposals:
        try:
            filename = item["filename"]
            name = item["name"]
            description = item["description"]
            mtype = item["type"]
            body = item["body"]
        except (KeyError, TypeError):
            continue
        if mtype not in {"user", "feedback", "project", "reference"}:
            continue
        try:
            memory.save(
                filename=filename,
                name=name,
                description=description,
                type=mtype,
                body=body,
                index_line=f"- [{name}]({filename}) — {description}",
            )
            saved.append(filename)
        except Exception as e:  # noqa: BLE001
            logger.warning("could not save memory %s: %s", filename, e)
    return saved


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(value)


def reset_state_for_tests() -> None:
    _STATE.enabled = False
    _STATE.memory = None
    _STATE.model = None
    _STATE.last_run_turn = 0
