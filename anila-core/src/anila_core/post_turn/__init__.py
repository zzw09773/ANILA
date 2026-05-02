"""Post-turn hooks for QueryEngine.

Hooks here run after a turn completes, fire-and-forget, and may not block
the main reply latency. See :class:`anila_core.engine.query_engine.QueryEngine`
for hook lifecycle.

The package mirrors how Claude Code wires "after-turn services": memory
extraction, session memory, prompt suggestion, etc.
"""

from .prompt_suggestion import PromptSuggestion, make_prompt_suggestion_hook

__all__ = ["PromptSuggestion", "make_prompt_suggestion_hook"]
