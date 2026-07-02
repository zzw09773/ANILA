"""Backwards-compat shim — canonical location is
``long_term.backends.filesystem.consolidator``.
"""
from .long_term.backends.filesystem.consolidator import (  # noqa: F401
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
