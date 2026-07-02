"""Backwards-compat shim — canonical location is ``long_term``.

Phase 1 of the route-3 restructure shipped a ``user/`` subpackage
that has since been hoisted into ``long_term/`` (multi-tenant,
filesystem + postgres backends). This shim re-exports everything
that lived under the old path so any caller still doing
``from anila_core.memory.user import X`` keeps working through
the cutover.
"""
from .long_term import (  # noqa: F401
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

__all__ = [
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
]
