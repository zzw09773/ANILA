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
from ..prompts import LANGUAGE_PREAMBLE
from ..prompts.sampling import get_sampling
from ..providers.base import Provider, ProviderRequest
from ..providers.guards import bumped_max_tokens, is_empty_length_failure

logger = logging.getLogger(__name__)


# 輕量前導（身分＋語言規則）＋任務指令。原版是英文＋英文範例，
# 小模型會照範例的語言出英文 chips——範例即規格，所以範例必須是繁中。
# 注意：本字串會過 str.format(n=...)，前導與範例內不得出現字面 {}。
_DEFAULT_SYSTEM_PROMPT = LANGUAGE_PREAMBLE + (
    "\n\n【任務】讀完以下對話，猜測使用者接下來最可能想問的 {n} 個追問問題。"
    "只輸出一個緊湊的 JSON 字串陣列，每項是一個完整的繁體中文問題，"
    "不加編號、不加項目符號、陣列前後不得有任何其他文字。範例："
    '["這個方法的誤差來源是什麼？", "有沒有對應的測試數據？", "這個結論適用於哪些條件？"]'
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
        # 預設來自 TASK_SAMPLING["chips"]（設計文件 §6-5／§9b：思考型模型地板）。
        max_tokens: Optional[int] = None,
        system_prompt: Optional[str] = None,
    ) -> None:
        self._provider = provider
        self._model = model
        self._n = max(1, min(8, n_suggestions))
        chips = get_sampling("chips")
        self._max_tokens = (
            max_tokens if max_tokens is not None else chips.max_tokens
        )
        self._temperature = chips.temperature
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
        max_tokens = self._max_tokens
        for attempt in range(2):
            request = ProviderRequest(
                model=self._model,
                system=self._system,
                messages=[UserMessage(content=focus_window)],
                tools=[],
                max_tokens=max_tokens,
                temperature=self._temperature,
            )
            text_chunks: list[str] = []
            finish_reason: str | None = None
            async for delta in self._provider.stream_completion(request):
                if delta.type == "text" and delta.text:
                    text_chunks.append(delta.text)
                elif delta.type == "stop":
                    finish_reason = delta.finish_reason
            raw = "".join(text_chunks)
            if raw.strip():
                return _parse_suggestions(raw, limit=self._n)
            # Only bump+retry when the model burned the budget (length + empty).
            # Legitimate empty end_turn / stop must not cost a second call.
            if attempt == 0 and is_empty_length_failure(finish_reason, raw):
                max_tokens = bumped_max_tokens(max_tokens)
                continue
            if is_empty_length_failure(finish_reason, raw):
                logger.warning(
                    "chips 連續兩次空回覆 — 疑似思考預算吃光？（thinking budget）"
                )
            return []
        return []


def make_prompt_suggestion_hook(
    provider: Provider,
    *,
    model: str,
    n_suggestions: int = 3,
    max_tokens: Optional[int] = None,
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
