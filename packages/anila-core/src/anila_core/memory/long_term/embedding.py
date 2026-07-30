"""Embedding contract for user-tenant memory chunks.

The platform stores into a ``halfvec(4000)`` HNSW column — pgvector's
halfvec HNSW max dim. The designated platform embedding model's *native*
output dimension is measured at designation time; vectors are then
normalised to the column width:

* native == 4000 → passthrough
* native < 4000 → zero-pad (``pad_from`` must equal the measured native
  dim). Cosine similarity is mathematically identical to native-dim
  search because padding zeros change neither the dot product nor either
  norm.
* native > 4000 → truncate the Matryoshka tail (NV-embed-V2's 4096 is
  the historical case). The UI must warn at designation time.

``pad_from`` is required for any adaptation other than the legacy
4096→4000 path. An earlier revision padded every short vector
unconditionally; that silently accepted an endpoint that drifted to a
different model. Dimension is the only signal that catches real drift,
so padding is a deliberate configuration act, not a silent fallback.

These constants are the single source of truth. CSP's
``PostgresMemoryAdapter`` and the ingestion-worker's ``Embedder`` both
read from here.
"""
from __future__ import annotations

from typing import Sequence


# Storage column width (matches migration 0015 / document_chunks).
EMBED_DIM = 4000

# Historical NV-embed-V2 native width. Kept so callers that have not yet
# been wired to a measured ``pad_from`` still truncate the known case.
EMBED_NATIVE_DIM = 4096

# Fallback name only — runtime resolution goes through the platform's
# ``is_platform_embedding`` designation, not this string.
DEFAULT_EMBED_MODEL = "nvidia/NV-embed-V2"


def truncate_embedding(
    vec: Sequence[float], *, pad_from: int | None = None
) -> list[float]:
    """Normalise an embedding vector to the storage column width.

    - ``EMBED_DIM`` (4000-d): passthrough.
    - exactly ``pad_from`` dims when ``pad_from > EMBED_DIM``: truncate.
    - exactly ``pad_from`` dims when ``0 < pad_from < EMBED_DIM``: zero-pad.
    - ``EMBED_NATIVE_DIM`` (4096-d) without ``pad_from``: truncate
      (legacy NV-embed-V2 path).
    - anything else raises.

    Why ``pad_from`` is required rather than accepting *any* short vector
    -------------------------------------------------------------------
    Unconditional padding turns a loud dimension mismatch into a silent
    corruption: a drifted endpoint serving a different-width model would
    land syntactically valid but semantically meaningless rows in the
    index. Declaring the source dimension keeps adaptation possible
    while any *other* width still fails loudly.
    """
    n = len(vec)
    if n == EMBED_DIM:
        return list(vec)
    if pad_from is not None and n == pad_from:
        if n > EMBED_DIM:
            return list(vec[:EMBED_DIM])
        if 0 < n < EMBED_DIM:
            return list(vec) + [0.0] * (EMBED_DIM - n)
    if pad_from is None and n == EMBED_NATIVE_DIM:
        return list(vec[:EMBED_DIM])
    accepted = f"{{{EMBED_DIM}, {EMBED_NATIVE_DIM}}}"
    if pad_from is not None:
        accepted = (
            f"{{{EMBED_DIM}, {EMBED_NATIVE_DIM}, "
            f"{pad_from} (declared pad_from)}}"
        )
    raise ValueError(
        f"anila_core.memory.user: embedding dim {n} not in {accepted}. "
        "A non-4000/4096 model must be declared explicitly (pad_from) so "
        "an endpoint silently serving a different model still fails loudly."
    )
