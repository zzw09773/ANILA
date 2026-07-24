"""Embedding contract for user-tenant memory chunks.

The platform deploys NVIDIA's NV-embed-V2 (4096-d native) and stores
into a ``halfvec(4000)`` HNSW column — pgvector's halfvec HNSW max
dim. Truncating the trailing 96 dims sits below NV-embed-V2's
Matryoshka noise floor, so retrieval quality is preserved.

These constants are the single source of truth. Any storage backend
or embed-client must use them; the CSP ``PostgresMemoryAdapter`` and
the ingestion-worker's ``Embedder`` both read from here so a future
move to a different embedder model only flips one symbol.
"""
from __future__ import annotations

from typing import Sequence


# Storage column width (matches migration 0030 / document_chunks).
EMBED_DIM = 4000

# What NV-embed-V2 returns natively. We truncate the tail because
# the OpenAI ``dimensions`` request param isn't honoured by the
# proxy in front of the embedder.
EMBED_NATIVE_DIM = 4096

# Default embedder model name. Operators override per-deployment via
# the ``MEMORY_EMBEDDING_MODEL`` env on the storage backend; this
# constant is the fallback when nothing is set.
DEFAULT_EMBED_MODEL = "nvidia/NV-embed-V2"


def truncate_embedding(vec: Sequence[float]) -> list[float]:
    """Normalise an embedding vector to the storage column width.

    - ``EMBED_DIM`` (4000-d): passthrough.
    - ``EMBED_NATIVE_DIM`` (4096-d): truncate the Matryoshka tail
      (legacy NV-embed-V2 path).
    - shorter than ``EMBED_DIM``: zero-pad up to the column width.
      Padding zeros contribute nothing to dot products or norms, so
      cosine similarity between same-model vectors is preserved
      exactly; smaller-dim models (e.g. 2048-d nemotron-3-embed-1b)
      adapt to the fixed ``halfvec(4000)`` column without migrations.
      Cross-model mixing inside one collection remains forbidden and
      is guarded by the collection embedding fingerprint, not here.
    - any other overlong vector still raises: blind truncation of a
      non-Matryoshka model would silently corrupt retrieval.
    """
    n = len(vec)
    if n == EMBED_DIM:
        return list(vec)
    if n == EMBED_NATIVE_DIM:
        return list(vec[:EMBED_DIM])
    if 0 < n < EMBED_DIM:
        return list(vec) + [0.0] * (EMBED_DIM - n)
    raise ValueError(
        f"anila_core.memory.user: embedding dim {n} not in "
        f"{{{EMBED_DIM}, {EMBED_NATIVE_DIM}}} and not paddable "
        f"(1..{EMBED_DIM - 1})"
    )
