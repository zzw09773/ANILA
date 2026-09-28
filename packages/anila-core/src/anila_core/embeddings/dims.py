"""Store an embedding at its native width, unless halfvec HNSW cannot index it.

pgvector's HNSW index on ``halfvec`` accepts at most 4000 dimensions.
Shorter models are stored as-is (no zero-pad). Longer models keep the
historical tail truncation and record both the native width and the
width that was actually stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


# pgvector halfvec HNSW ceiling. Also the historical column width.
HALFVEC_HNSW_MAX = 4000

_SUBJECTS = frozenset({"chunk", "fact", "summary"})


@dataclass(frozen=True)
class StoredVector:
    values: list[float]
    dims: int
    native_dims: int

    @property
    def truncated(self) -> bool:
        return self.dims < self.native_dims


def fit_stored_vector(
    vec: Sequence[float],
    *,
    declared_native: int | None = None,
) -> StoredVector:
    """Fit ``vec`` for storage.

    ``declared_native`` is the width measured for this model. A vector
    of any other width is refused so a drifted endpoint cannot land in
    the same index. ``None`` accepts the vector's own width (and the
    historical 4096→4000 truncation when nothing was measured).
    """
    values = [float(x) for x in vec]
    n = len(values)
    if n <= 0:
        raise ValueError("embedding is empty")
    if declared_native is not None:
        if declared_native <= 0:
            raise ValueError(f"declared native dim {declared_native} is not positive")
        already_truncated = (
            declared_native > HALFVEC_HNSW_MAX and n == HALFVEC_HNSW_MAX
        )
        if n != declared_native and not already_truncated:
            raise ValueError(
                f"embedding dim {n} != declared native {declared_native}"
            )
        native = declared_native
    else:
        native = n
    if n > HALFVEC_HNSW_MAX:
        stored = values[:HALFVEC_HNSW_MAX]
        return StoredVector(stored, HALFVEC_HNSW_MAX, native)
    return StoredVector(values, n, native)


def ann_index_sql(*, model_id: int, dims: int, subject: str) -> str:
    """Partial HNSW index for one model's stored width.

    Identifiers are interpolated only after they are checked as ints or
    an allow-listed subject, so this string is safe to execute.
    """
    if isinstance(model_id, bool) or not isinstance(model_id, int) or model_id <= 0:
        raise ValueError(f"model_id must be a positive int, got {model_id!r}")
    if isinstance(dims, bool) or not isinstance(dims, int):
        raise ValueError(f"dims must be an int, got {dims!r}")
    if dims < 1 or dims > HALFVEC_HNSW_MAX:
        raise ValueError(
            f"dims {dims} cannot be indexed (halfvec HNSW max {HALFVEC_HNSW_MAX})"
        )
    if subject not in _SUBJECTS:
        raise ValueError(f"subject must be one of {sorted(_SUBJECTS)}")
    name = f"ix_embvec_hnsw_{subject}_{model_id}_{dims}"
    return (
        f"CREATE INDEX IF NOT EXISTS {name} ON embedding_vectors "
        f"USING hnsw ((embedding::halfvec({dims})) halfvec_cosine_ops) "
        f"WITH (m = 16, ef_construction = 64) "
        f"WHERE model_id = {model_id} AND subject = '{subject}' AND dims = {dims}"
    )
