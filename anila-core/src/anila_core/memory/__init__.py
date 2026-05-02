"""Memory lifecycle — extraction, relevance selection, consolidation, memdir, session.

Three layers live here, kept conceptually separate even though they share
the directory:

- ``memdir`` / ``extract_memories`` / ``relevance_selector`` / ``consolidation``
  — long-term cross-session facts.
- ``compact/`` (sibling package) — token-budget control inside one turn loop.
- ``session`` / ``memory_session`` / ``sqlite_session`` — conversation history
  + pending interrupts for **one** chat session (Sprint 9).
"""

from .extract_memories import MemoryExtractor
from .relevance_selector import ModelBasedRelevanceSelector, RelevantMemory
from .consolidation import ConsolidationService
from .memdir import MemdirManager, ENTRYPOINT_NAME, MAX_ENTRYPOINT_LINES
from .session import (
    InterruptRecord,
    Session,
    new_interrupt_id,
    new_session_id,
)
from .memory_session import MemorySession
from .sqlite_session import SqliteSession, close_all_connections

__all__ = [
    "MemoryExtractor",
    "ModelBasedRelevanceSelector",
    "RelevantMemory",
    "ConsolidationService",
    "MemdirManager",
    "ENTRYPOINT_NAME",
    "MAX_ENTRYPOINT_LINES",
    # Session protocol + adapters (Sprint 9 PR 1)
    "Session",
    "InterruptRecord",
    "MemorySession",
    "SqliteSession",
    "close_all_connections",
    "new_session_id",
    "new_interrupt_id",
]
