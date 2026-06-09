"""anila-core memory module — short-term + long-term memory primitives.

Restructured into a clear taxonomy:

* :mod:`anila_core.memory.short_term` — within-conversation working
  state (Session Protocol + in-memory / sqlite adapters).
* :mod:`anila_core.memory.long_term` — cross-session facts and
  retrieval. DTOs, extraction pipeline, embedding contract, and
  the storage adapter Protocol live here. Concrete backends live
  under :mod:`long_term.backends`:
  - ``backends.filesystem`` — original ``MemdirManager`` family
    (per-agent, MEMORY.md + frontmatter MD docs).
  - ``backends.postgres`` — contract-only stub; the concrete
    ``PostgresMemoryAdapter`` ships in CSP.
* :mod:`anila_core.memory.compact` — token-budget control inside a
  single turn loop (sibling package; not part of the short/long
  taxonomy because it operates on prompt construction, not
  storage).

Backwards-compatible top-level re-exports below cover both the new
taxonomy and the legacy import paths
(``anila_core.memory.session``, ``anila_core.memory.memdir``, ...)
which now resolve through shims under their old names. New code
should import from the canonical sub-package.
"""

# Submodule namespaces (preferred for new code)
from . import short_term, long_term

# ── Short-term re-exports (legacy compat + convenience) ───────────────────────
from .short_term import (
    InterruptRecord,
    MemorySession,
    Session,
    SqliteSession,
    close_all_connections,
    new_interrupt_id,
    new_session_id,
)

# ── Long-term re-exports — canonical user-tenant API ──────────────────────────
from .long_term import (
    DEFAULT_EMBED_MODEL,
    EMBED_DIM,
    EMBED_NATIVE_DIM,
    EXTRACTION_SYSTEM_PROMPT,
    MemoryAdapter,
    MemoryReadResult,
    RetrievedChunk,
    UserFactDTO,
    format_transcript_for_extraction,
    parse_extraction_response,
    truncate_embedding,
)

# ── Long-term re-exports — legacy memdir family (filesystem backend) ──────────
from .long_term.backends.filesystem import (
    ConsolidationService,
    ENTRYPOINT_NAME,
    MAX_ENTRYPOINT_LINES,
    MemdirManager,
    MemoryExtractor,
    ModelBasedRelevanceSelector,
    RelevantMemory,
)

__all__ = [
    # Submodule namespaces
    "short_term",
    "long_term",
    # Short-term (Session Protocol + adapters)
    "InterruptRecord",
    "MemorySession",
    "Session",
    "SqliteSession",
    "close_all_connections",
    "new_interrupt_id",
    "new_session_id",
    # Long-term — user-tenant canonical API
    "DEFAULT_EMBED_MODEL",
    "EMBED_DIM",
    "EMBED_NATIVE_DIM",
    "EXTRACTION_SYSTEM_PROMPT",
    "MemoryAdapter",
    "MemoryReadResult",
    "RetrievedChunk",
    "UserFactDTO",
    "format_transcript_for_extraction",
    "parse_extraction_response",
    "truncate_embedding",
    # Long-term — filesystem (legacy memdir) backend
    "ConsolidationService",
    "ENTRYPOINT_NAME",
    "MAX_ENTRYPOINT_LINES",
    "MemdirManager",
    "MemoryExtractor",
    "ModelBasedRelevanceSelector",
    "RelevantMemory",
]
