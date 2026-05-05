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

    Accepts either the native 4096-d output (truncates) or an
    already-4000-d vector (passthrough). Anything else raises so a
    misconfigured embedder fails loudly at write time rather than
    populating the column with garbage that can't be ANN-searched.
    """
    n = len(vec)
    if n == EMBED_DIM:
        return list(vec)
    if n == EMBED_NATIVE_DIM:
        return list(vec[:EMBED_DIM])
    raise ValueError(
        f"anila_core.memory.user: embedding dim {n} not in "
        f"{{{EMBED_DIM}, {EMBED_NATIVE_DIM}}}"
    )
