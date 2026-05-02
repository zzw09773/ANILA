"""ToolOutputTrimmerMiddleware — sliding-window cap on tool result size.

The motivating problem: a ``vector_search`` Action that returns 50
chunks of 2KB each emits a 100KB tool result. After 5 turns of agentic
search, the conversation history holds 500KB of tool output that the
LLM has already consumed and integrated. Each subsequent turn re-pays
the input-token tax for context the LLM no longer needs at full
fidelity.

Strategy: a middleware that wraps tool calls and, after the handler
returns, **rewrites the result** to a compact preview if the size
exceeds a threshold. This middleware does NOT modify the previous
turn's tool output (the conversation history is owned by the runner);
it only intervenes at result-emission time.

Combined with ``message_history_trimmer`` (a separate middleware that
walks the history at LLM-call time — out of scope for v0.1 Sprint 2;
lands with the StateMachine), the agent's full-fidelity context stays
bounded even on long sessions.

Configuration knobs:

- ``max_chars`` — threshold above which the result gets previewed.
  Default 2000 (≈500 tokens — comfortable for one or two retrieval
  results to pass through unmodified, anything larger gets capped).
- ``preview_chars`` — how much head text to keep in the preview.
  Default 600 (~150 tokens).
- ``trim_tools`` — name allowlist. Only these tools' outputs get
  considered. Default ``None`` = all tools.
- ``preserve_recent_turns`` — number of turns at the END of the run
  to leave at full fidelity. v0.1 implementation note: this kicks in
  for the in-flight call only; the cross-turn window awaits the
  StateMachine refactor.

Local-deployment relevance: ANILA's vLLM context windows are smaller
than cloud APIs (Gemma-2 8K is common). Trimming aggressively is the
single biggest lever for fitting a multi-turn RAG conversation in
that window without LLM-side compaction overhead.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agentic_rag.runtime.framework.action import (
    Action,
    ActionContext,
    ActionResult,
)
from agentic_rag.runtime.framework.middleware.protocol import NextHandler

logger = logging.getLogger(__name__)


_DEFAULT_MAX_CHARS = 2_000
_DEFAULT_PREVIEW_CHARS = 600
_TRIM_MARKER = "…[output trimmed by middleware: kept first {kept}/{total} chars]"


class ToolOutputTrimmerMiddleware:
    """Middleware that caps tool output size after handler return.

    Construction:

        ToolOutputTrimmerMiddleware(
            max_chars=2000,           # output above this gets trimmed
            preview_chars=600,        # how much head text to keep
            trim_tools=("vector_search", "keyword_search"),  # allowlist
        )

    The middleware never trims errors — error messages are usually
    short and always informative. Structured outputs (dict / list)
    are JSON-serialised for size measurement; if they exceed the
    threshold, they're replaced with a preview dict
    ``{"_trimmed": True, "preview": "...", "original_size_chars": N}``
    that LLMs handle gracefully and the runner stringifies into the
    tool message naturally.
    """

    def __init__(
        self,
        *,
        max_chars: int = _DEFAULT_MAX_CHARS,
        preview_chars: int = _DEFAULT_PREVIEW_CHARS,
        trim_tools: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        if max_chars < 1:
            raise ValueError(f"max_chars must be positive, got {max_chars}")
        if preview_chars < 1 or preview_chars >= max_chars:
            raise ValueError(
                f"preview_chars ({preview_chars}) must be in [1, max_chars={max_chars})"
            )
        self._max = max_chars
        self._preview = preview_chars
        self._allowlist = (
            None if trim_tools is None else frozenset(trim_tools)
        )

    async def __call__(
        self,
        action: Action,
        context: ActionContext,
        next_: NextHandler,
    ) -> ActionResult:
        result = await next_(context)

        if result.is_error:
            return result
        if self._allowlist is not None and action.name not in self._allowlist:
            return result
        if result.output is None:
            return result

        return self._maybe_trim(result, action.name)

    # ── helpers ──────────────────────────────────────────────────────

    def _maybe_trim(self, result: ActionResult, action_name: str) -> ActionResult:
        rendered = self._render_for_size_check(result.output)
        if rendered is None:
            return result
        if len(rendered) <= self._max:
            return result

        preview = rendered[: self._preview]
        marker = _TRIM_MARKER.format(kept=self._preview, total=len(rendered))

        # Preserve the structural shape: if the original was a string,
        # return a string; if a dict / list, return a structured preview
        # dict so downstream JSON serialisation stays lossless.
        new_output: Any
        if isinstance(result.output, str):
            new_output = preview + "\n" + marker
        else:
            new_output = {
                "_trimmed": True,
                "preview": preview,
                "original_size_chars": len(rendered),
                "trim_marker": marker,
            }

        logger.debug(
            "output_trimmer: %s output trimmed %d → %d chars",
            action_name,
            len(rendered),
            self._preview,
        )
        return ActionResult(
            output=new_output,
            metadata={
                **result.metadata,
                "_output_trimmed": True,
                "_original_size_chars": len(rendered),
            },
            handoff_target=result.handoff_target,
        )

    @staticmethod
    def _render_for_size_check(output: Any) -> str | None:
        """Stringify ``output`` for length comparison. ``None`` if too
        opaque to measure."""
        if output is None:
            return None
        if isinstance(output, str):
            return output
        try:
            return json.dumps(output, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(output)


__all__ = ["ToolOutputTrimmerMiddleware"]
