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


def truncate_embedding(
    vec: Sequence[float], *, pad_from: int | None = None
) -> list[float]:
    """Normalise an embedding vector to the storage column width.

    - ``EMBED_DIM`` (4000-d): passthrough.
    - ``EMBED_NATIVE_DIM`` (4096-d): truncate the Matryoshka tail
      (legacy NV-embed-V2 path).
    - exactly ``pad_from`` dims (when the caller declares it): zero-pad
      up to the column width.  Padding zeros contribute nothing to dot
      products or norms, so cosine similarity between same-model vectors
      is preserved exactly; a smaller-dim model (e.g. 2048-d
      nemotron-3-embed-1b) therefore adapts to the fixed
      ``halfvec(4000)`` column without a migration.
    - anything else raises.

    Why ``pad_from`` is required rather than accepting *any* short vector
    -------------------------------------------------------------------
    An earlier revision padded every ``0 < n < EMBED_DIM`` unconditionally.
    That silently re-opened the exact hole this function exists to close:
    if the configured endpoint starts serving a **different** model (say a
    1536-d one), its vectors would be zero-padded into syntactically valid
    but semantically meaningless rows, land in the index, and destroy
    retrieval quality with no error anywhere.  Two CI suites caught it
    (``ingestion-worker`` and ``Python security contracts``), and the
    ingestion embedder's own comment already stated the invariant:
    *"an arbitrary vector must never be silently sliced into a
    syntactically valid but semantically corrupt database value."*

    The collection ``embedding_fingerprint`` does **not** cover this: it
    guards the *declared* model identity, so it cannot detect an endpoint
    that quietly serves something else under the same name.  Dimension was
    the only signal that caught real drift.

    Making the source dimension explicit keeps both properties:
    adapting to a smaller model stays possible, but it becomes a
    deliberate configuration act, and any *other* dimension still fails
    loudly.
    """
    n = len(vec)
    if n == EMBED_DIM:
        return list(vec)
    if n == EMBED_NATIVE_DIM:
        return list(vec[:EMBED_DIM])
    if pad_from is not None and n == pad_from and 0 < n < EMBED_DIM:
        return list(vec) + [0.0] * (EMBED_DIM - n)
    accepted = f"{{{EMBED_DIM}, {EMBED_NATIVE_DIM}}}"
    if pad_from is not None:
        accepted = f"{{{EMBED_DIM}, {EMBED_NATIVE_DIM}, {pad_from} (declared, padded)}}"
    raise ValueError(
        f"anila_core.memory.user: embedding dim {n} not in {accepted}. "
        "A smaller-dim model must be declared explicitly (pad_from) so that "
        "an endpoint silently serving a different model still fails loudly."
    )
