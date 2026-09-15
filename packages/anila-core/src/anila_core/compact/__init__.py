"""Compact services — micro compact, auto compact, sliding window, and session memory."""

from .micro_compact import micro_compact_messages, TIME_BASED_MC_CLEARED_MESSAGE, COMPACTABLE_TOOLS
from .auto_compact import (
    AUTOCOMPACT_BUFFER_TOKENS,
    FALLBACK_CONTEXT_WINDOW,
    MAX_OUTPUT_TOKENS_FOR_SUMMARY,
    should_compact,
)
from .openai_history import (
    CompactResult,
    HISTORY_SUMMARY_PREFIX,
    auto_compact_openai_messages,
    estimate_openai_tokens,
    is_prompt_too_long,
    sliding_window_openai,
)
from .sliding_window import SLIDING_WINDOW_SUMMARY, sliding_window_compact
from .session_memory import SessionMemoryConfig, SessionMemoryService
from .strip_images import (
    IMAGE_MAX_TOKENS,
    IMAGE_MIN_TOKENS,
    strip_images_messages,
    strip_images_openai,
)

__all__ = [
    "micro_compact_messages",
    "TIME_BASED_MC_CLEARED_MESSAGE",
    "COMPACTABLE_TOOLS",
    "should_compact",
    "MAX_OUTPUT_TOKENS_FOR_SUMMARY",
    "AUTOCOMPACT_BUFFER_TOKENS",
    "FALLBACK_CONTEXT_WINDOW",
    "sliding_window_compact",
    "SLIDING_WINDOW_SUMMARY",
    "SessionMemoryService",
    "SessionMemoryConfig",
    "CompactResult",
    "HISTORY_SUMMARY_PREFIX",
    "auto_compact_openai_messages",
    "estimate_openai_tokens",
    "is_prompt_too_long",
    "sliding_window_openai",
    "IMAGE_MAX_TOKENS",
    "IMAGE_MIN_TOKENS",
    "strip_images_messages",
    "strip_images_openai",
]
