"""``PromptSuggestion`` — generate 3 follow-up question chips after each turn.

Mirrors Claude Code's ``services/PromptSuggestion`` (without the CLI
chrome). Runs as a non-blocking post-turn hook on QueryEngine; talks to
a Provider with a tiny prompt that asks for a JSON array of strings.

Wire-up::

    engine.add_post_turn_hook(
        make_prompt_suggestion_hook(provider, model="local-small")
    )

When an :class:`AgentContext.event_emitter` is bound (server.py installs
one — Sprint 9 PR 4), the resulting suggestions are pushed as the
``follow_ups`` SSE event so the web UI can render chips. Failures are
swallowed — the chips are nice-to-have, never block the user reply.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from ..context.agent_context import get_current_context
from ..engine.query_engine import PostTurnHook, TurnResult
from ..models.message import Message, UserMessage
from ..providers.base import Provider, ProviderRequest

logger = logging.getLogger(__name__)


_DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant that suggests follow-up questions a "
    "user might ask next. Read the conversation, then output ONLY a "
    "compact JSON array of {n} short strings (each one a complete "
    "question, no leading bullet, no commentary). Example: "
    '["What about edge cases?", "Can you show a test?", "How does that scale?"]'
)


class PromptSuggestion:
    """Stateless suggester. Reusable across turns; safe to share across runs.

    Args:
        provider: LLM provider used to generate the suggestions.
        model: Model identifier passed to the provider.
        n_suggestions: How many chips to ask the model for. The result
            is best-effort — fewer is fine, more is truncated.
        max_tokens: Hard cap on the suggestion call to keep latency low.
        system_prompt: Override the default system prompt.
    """

    def __init__(
        self,
        *,
        provider: Provider,
        model: str,
        n_suggestions: int = 3,
        max_tokens: int = 200,
        system_prompt: Optional[str] = None,
    ) -> None:
        self._provider = provider
        self._model = model
        self._n = max(1, min(8, n_suggestions))
        self._max_tokens = max_tokens
        self._system = (system_prompt or _DEFAULT_SYSTEM_PROMPT).format(
            n=self._n
        )

    async def __call__(self, result: TurnResult) -> None:
        """Hook entrypoint. Always swallows exceptions."""
        if not _is_eligible(result):
            return
        try:
            suggestions = await self._suggest(result.messages)
        except Exception as exc:
            logger.warning("PromptSuggestion failed: %s", exc)
            return
        if not suggestions:
            return
        ctx = get_current_context()
        if ctx is not None and ctx.event_emitter is not None:
            try:
                await ctx.event_emitter(
                    "follow_ups", {"suggestions": suggestions}
                )
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("event_emitter('follow_ups') raised: %s", exc)

    async def _suggest(self, history: list[Message]) -> list[str]:
        # Use a short summary of the last few turns rather than the full
        # history — keeps the suggestion call cheap and focused.
        focus_window = _build_focus_window(history)
        request = ProviderRequest(
            model=self._model,
            system=self._system,
            messages=[UserMessage(content=focus_window)],
            tools=[],
            max_tokens=self._max_tokens,
            temperature=0.4,
        )
        text_chunks: list[str] = []
        async for delta in self._provider.stream_completion(request):
            if delta.type == "text" and delta.text:
                text_chunks.append(delta.text)
        return _parse_suggestions("".join(text_chunks), limit=self._n)


def make_prompt_suggestion_hook(
    provider: Provider,
    *,
    model: str,
    n_suggestions: int = 3,
    max_tokens: int = 200,
) -> PostTurnHook:
    """Convenience factory mirroring the engine's hook signature."""
    suggester = PromptSuggestion(
        provider=provider,
        model=model,
        n_suggestions=n_suggestions,
        max_tokens=max_tokens,
    )

    async def _hook(result: TurnResult) -> None:
        await suggester(result)

    return _hook


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _is_eligible(result: TurnResult) -> bool:
    """Skip suggestion when the turn errored, paused, or was empty."""
    if result.stop_reason not in ("completed", "max_turns"):
        return False
    if not result.messages:
        return False
    last = result.messages[-1]
    return last.role == "assistant"


def _build_focus_window(history: list[Message], window: int = 6) -> str:
    """Render the last ``window`` turns as plain text for the suggester."""
    recent = history[-window:]
    parts: list[str] = []
    for msg in recent:
        text = msg.get_text() if hasattr(msg, "get_text") else str(msg)
        text = text.strip()
        if text:
            parts.append(f"{msg.role}: {text}")
    return "\n\n".join(parts)


def _parse_suggestions(text: str, *, limit: int) -> list[str]:
    """Best-effort JSON-array extraction from the model's reply.

    Tolerates leading / trailing prose by scanning for the first ``[``
    and matching ``]`` rather than requiring a clean response. Returns
    an empty list when nothing parseable is found — callers treat that
    as "no chips this turn".
    """
    if not text:
        return []
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        return []
    blob = text[start : end + 1]
    try:
        items = json.loads(blob)
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []
    cleaned: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        s = item.strip()
        if s:
            cleaned.append(s)
        if len(cleaned) >= limit:
            break
    return cleaned


# Re-export :class:`PostTurnHook` so callers don't need to dig into
# the engine module just to type their own hooks.
__all__ = ["PromptSuggestion", "make_prompt_suggestion_hook"]


# Keep mypy happy on the Any-typed shim.
_: Any = PostTurnHook  # noqa: F841
