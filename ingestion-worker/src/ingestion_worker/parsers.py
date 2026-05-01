"""Back-compat re-export of ``anila_core.ingestion.parsers.extract_text``.

The thin wrapper that previously lived here moved to ``anila-core``
(Pillar 2 shared infrastructure) on Sprint 8 X / chunking-preview
Phase 1 so CSP's ``POST /api/ingestion/chunking-preview`` endpoint
can call ``extract_text`` synchronously without going through the
Arq queue.

Existing call sites (``handlers.py``, ``evaluator.py``) continue to
work unchanged — the symbol is re-exported here. New code should
import directly from ``anila_core.ingestion.parsers`` instead.
"""

from anila_core.ingestion.parsers import extract_text

__all__ = ["extract_text"]
