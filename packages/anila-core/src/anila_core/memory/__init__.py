"""anila-core memory module — short-term + long-term memory primitives.

Restructured under route 3 of the anila-memory-layer-rfc into a
clear taxonomy:

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

# 2026-09-02: lazy (PEP 562). The Router needs ``memory.short_term`` and
# ``memory.contract`` only; the eager re-exports used to load the filesystem
# long-term backend (extractor / consolidator / selector) into every process.
from __future__ import annotations

import importlib

_SUBMODULES = ("short_term", "long_term", "contract")
_LAZY: dict[str, str] = {
    # short_term
    "InterruptRecord": ".short_term",
    "MemorySession": ".short_term",
    "Session": ".short_term",
    "SqliteSession": ".short_term",
    "close_all_connections": ".short_term",
    "new_interrupt_id": ".short_term",
    "new_session_id": ".short_term",
    # long_term
    "DEFAULT_EMBED_MODEL": ".long_term",
    "EMBED_DIM": ".long_term",
    "EMBED_NATIVE_DIM": ".long_term",
    "EXTRACTION_SYSTEM_PROMPT": ".long_term",
    "MemoryAdapter": ".long_term",
    "MemoryReadResult": ".long_term",
    "RetrievedChunk": ".long_term",
    "UserFactDTO": ".long_term",
    "format_transcript_for_extraction": ".long_term",
    "parse_extraction_response": ".long_term",
    "truncate_embedding": ".long_term",
    # long_term filesystem backend
    "ConsolidationService": ".long_term.backends.filesystem",
    "ENTRYPOINT_NAME": ".long_term.backends.filesystem",
    "MAX_ENTRYPOINT_LINES": ".long_term.backends.filesystem",
    "MemdirManager": ".long_term.backends.filesystem",
    "MemoryExtractor": ".long_term.backends.filesystem",
    "ModelBasedRelevanceSelector": ".long_term.backends.filesystem",
    "RelevantMemory": ".long_term.backends.filesystem",
}

__all__ = list(_SUBMODULES) + sorted(_LAZY)


def __getattr__(name: str):
    if name in _SUBMODULES:
        module = importlib.import_module(f".{name}", __name__)
        globals()[name] = module
        return module
    try:
        modname = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(importlib.import_module(modname, __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY) | set(_SUBMODULES))
