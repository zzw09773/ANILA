"""Backwards-compat shim — canonical location is
``long_term.backends.filesystem.selector``.

``format_memory_manifest`` is also re-exported here because the
selector module historically owned a copy alongside ``memdir.py``;
preserving the alias keeps ``test_memory.py`` and any external
caller relying on the duplicated symbol working.
"""
from .long_term.backends.filesystem.selector import (  # noqa: F401
    MAX_RELEVANT_MEMORIES,
    SELECT_MEMORIES_SYSTEM_PROMPT,
    SIDE_QUERY_TIMEOUT,
    ModelBasedRelevanceSelector,
    RelevanceSelector,
    RelevantMemory,
    format_memory_manifest,
)

__all__ = [
    "MAX_RELEVANT_MEMORIES",
    "SELECT_MEMORIES_SYSTEM_PROMPT",
    "SIDE_QUERY_TIMEOUT",
    "ModelBasedRelevanceSelector",
    "RelevanceSelector",
    "RelevantMemory",
    "format_memory_manifest",
]
