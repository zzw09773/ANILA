"""Dimension-agnostic embeddings, blue-green rebuild, keyword fallback."""

from anila_core.embeddings.dims import (
    HALFVEC_HNSW_MAX,
    StoredVector,
    ann_index_sql,
    fit_stored_vector,
)
from anila_core.embeddings.keyword import (
    KEYWORD_FALLBACK_NOTICE,
    cjk_search_units,
    order_by_indexes,
    parse_rerank_indexes,
    search_path,
)
from anila_core.embeddings.swap import (
    OLD_VECTOR_RETENTION_DAYS,
    ActivationState,
    InMemoryCorpus,
    WorkItem,
    WriteTarget,
    cleanup_model_id,
    eta_seconds,
    plan_designation,
    plan_write_targets,
    rollback_state,
    run_rebuild_pass,
)

__all__ = [
    "HALFVEC_HNSW_MAX",
    "KEYWORD_FALLBACK_NOTICE",
    "OLD_VECTOR_RETENTION_DAYS",
    "ActivationState",
    "InMemoryCorpus",
    "StoredVector",
    "WorkItem",
    "WriteTarget",
    "ann_index_sql",
    "cjk_search_units",
    "cleanup_model_id",
    "eta_seconds",
    "fit_stored_vector",
    "order_by_indexes",
    "parse_rerank_indexes",
    "plan_designation",
    "plan_write_targets",
    "rollback_state",
    "run_rebuild_pass",
    "search_path",
]
