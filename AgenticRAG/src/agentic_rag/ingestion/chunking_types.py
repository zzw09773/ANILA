"""Chunker output type — minimal copy of ``ChunkResult`` from anila-core.

Phase 0 decoupling (2026-05-02): AgenticRAG's pgvector_store needs
``ChunkResult`` for its ``index_chunks(...)`` write path signature.
We copy only the data class, not the full ``ChunkerStrategy`` ABC —
strategy registration stays in anila-core / ingestion-worker land.

If a forked AgenticRAG ever wants to run chunking in-process (rather
than via the ingestion-worker), it can implement its own chunkers
that emit this type and call ``CollectionScopedPgVectorStore.
index_chunks`` directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ChunkResult:
    """One chunk produced by a chunking strategy.

    Frozen because the ingestion-worker passes the same instance across
    embedding / storage / audit stages — accidental mutation midway
    would break deterministic re-runs (the evaluator depends on this
    determinism to compare strategies).

    Attributes:
        content: The chunk text. Must be non-empty.
        chunk_key: Stable hierarchical id (e.g. ``"doc123/ch1/sec2/para5"``).
            Persisted into ``document_chunks.chunk_key``; participates in
            the ``UNIQUE (collection_id, chunk_key)`` constraint.
        token_count: Tokeniser-agnostic chunk size estimate. Advisory
            for UI display, not authoritative.
        metadata: Free-form audit context (heading path, page, parent
            chunk_key, source byte range, ...). JSON-serialisable.
    """

    content: str
    chunk_key: str
    token_count: int
    metadata: dict[str, Any] = field(default_factory=dict)
