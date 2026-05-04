"""Long-term memory — cross-session facts + retrieval.

Two complementary tenancies live here:

* **per-user** (the canonical tenant after route-3 unification) —
  platform-wide facts and RAG chunks scoped to a CSP user. Storage
  lives in CSP's Postgres; the contract sits in this package.
* **per-agent** (legacy ``MemdirManager`` family) — file-system
  MEMORY.md + frontmatter MD docs that an individual agent process
  builds up across its sessions. Stays here for SDK consumers; the
  user-tenant path is the recommended one for platform features.

Public API:

* DTOs: :class:`UserFactDTO`, :class:`RetrievedChunk`,
  :class:`MemoryReadResult` — what storage backends serialise to.
* Extraction pipeline: :data:`EXTRACTION_SYSTEM_PROMPT`,
  :func:`parse_extraction_response`,
  :func:`format_transcript_for_extraction`.
* Embedding contract: :data:`EMBED_DIM`, :data:`EMBED_NATIVE_DIM`,
  :data:`DEFAULT_EMBED_MODEL`, :func:`truncate_embedding`.
* Adapter: :class:`MemoryAdapter` Protocol — storage backends
  implement this; CSP ships ``PostgresMemoryAdapter`` (route 3
  Phase 2 cutover).

Backends live under ``backends/`` — currently ``filesystem`` (the
original memdir) and ``postgres`` (contract-only; concrete impl in
CSP).
"""
from .adapter import MemoryAdapter
from .clients import HttpUserFactReader, UserFactReadError
from .embedding import (
    DEFAULT_EMBED_MODEL,
    EMBED_DIM,
    EMBED_NATIVE_DIM,
    truncate_embedding,
)
from .extraction import (
    EXTRACTION_SYSTEM_PROMPT,
    format_transcript_for_extraction,
    parse_extraction_response,
)
from .models import (
    MemoryReadResult,
    RetrievedChunk,
    UserFactDTO,
)

__all__ = [
    "DEFAULT_EMBED_MODEL",
    "EMBED_DIM",
    "EMBED_NATIVE_DIM",
    "EXTRACTION_SYSTEM_PROMPT",
    "HttpUserFactReader",
    "MemoryAdapter",
    "MemoryReadResult",
    "RetrievedChunk",
    "UserFactDTO",
    "UserFactReadError",
    "format_transcript_for_extraction",
    "parse_extraction_response",
    "truncate_embedding",
]
