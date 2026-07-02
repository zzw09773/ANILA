"""Filesystem-backed long-term memory (the original memdir family).

Per-agent ``MEMORY.md`` + frontmatter MD docs. Each running agent
process owns its own memory directory; this is the SDK's native
storage model and predates the user-tenant cutover.

Public surface (re-exported here for one-import access):

* :class:`MemdirManager` — read/write the manifest + memory files.
* :class:`MemoryExtractor` — post-turn fork-subagent that decides
  what to write.
* :class:`ModelBasedRelevanceSelector` / :class:`RelevantMemory` —
  side-LLM call that picks which memories to surface for a given
  query.
* :class:`ConsolidationService` / :class:`ConsolidationConfig` /
  :class:`ConsolidationLockManager` — periodic dedup / merge of
  the memory directory.
"""
from .manager import (
    ENTRYPOINT_NAME,
    FRONTMATTER_MAX_LINES,
    MAX_ENTRYPOINT_BYTES,
    MAX_ENTRYPOINT_LINES,
    MAX_MEMORY_FILES,
    MemdirManager,
    format_memory_manifest,
    scan_memory_files,
    truncate_entrypoint_content,
)
from .extractor import (
    EXTRACT_PROMPT_TEMPLATE,
    EXTRACTION_ALLOWED_TOOLS,
    MemoryExtractor,
)
from .selector import (
    MAX_RELEVANT_MEMORIES,
    SELECT_MEMORIES_SYSTEM_PROMPT,
    SIDE_QUERY_TIMEOUT,
    ModelBasedRelevanceSelector,
    RelevanceSelector,
    RelevantMemory,
)
from .consolidator import (
    CONSOLIDATION_PROMPT_TEMPLATE,
    DEFAULT_MIN_HOURS,
    DEFAULT_MIN_SESSIONS,
    HOLDER_STALE_SECONDS,
    LOCK_FILE_NAME,
    ConsolidationConfig,
    ConsolidationLockManager,
    ConsolidationLockState,
    ConsolidationService,
    build_consolidation_prompt,
)

__all__ = [
    # manager
    "ENTRYPOINT_NAME",
    "FRONTMATTER_MAX_LINES",
    "MAX_ENTRYPOINT_BYTES",
    "MAX_ENTRYPOINT_LINES",
    "MAX_MEMORY_FILES",
    "MemdirManager",
    "format_memory_manifest",
    "scan_memory_files",
    "truncate_entrypoint_content",
    # extractor
    "EXTRACT_PROMPT_TEMPLATE",
    "EXTRACTION_ALLOWED_TOOLS",
    "MemoryExtractor",
    # selector
    "MAX_RELEVANT_MEMORIES",
    "SELECT_MEMORIES_SYSTEM_PROMPT",
    "SIDE_QUERY_TIMEOUT",
    "ModelBasedRelevanceSelector",
    "RelevanceSelector",
    "RelevantMemory",
    # consolidator
    "CONSOLIDATION_PROMPT_TEMPLATE",
    "DEFAULT_MIN_HOURS",
    "DEFAULT_MIN_SESSIONS",
    "HOLDER_STALE_SECONDS",
    "LOCK_FILE_NAME",
    "ConsolidationConfig",
    "ConsolidationLockManager",
    "ConsolidationLockState",
    "ConsolidationService",
    "build_consolidation_prompt",
]
