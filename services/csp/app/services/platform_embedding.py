"""Platform embedding model resolution (P4.8).

One designated ``model_registry`` row (``is_platform_embedding``) is the
single source of truth for:

* memory embed / retrieve
* new knowledge-collection defaults
* ingestion-worker embed requests

Mirrors the ``is_router_primary`` shape. Callers never match embedding
models by a hardcoded name string — that was the silent production
defect (``nvidia/NV-embed-V2`` vs registered ``nvidia/nv-embed-v2``).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from anila_core.memory.long_term import EMBED_DIM

from app.models.model_registry import ModelRegistry
from app.services.relation_resolver import scope_collection_rls

logger = logging.getLogger(__name__)

# Only reached when ``model_registry`` holds no active embedding row at
# all, so collection create never becomes a new gate. Spelled the way
# the model registers itself, NOT the way migration 0014's column
# default used to spell it (``nvidia/NV-embed-V2``) — that disagreement
# between two independently-written free-text fields is FAKE-CONTROLS
# #56, and every collection created off the old default committed it
# again.
LAST_RESORT_EMBEDDING_MODEL = "nvidia/nv-embed-v2"


@dataclass(frozen=True)
class PlatformEmbedding:
    """Resolved designation (or fallback) for embed callers."""

    model: ModelRegistry
    native_dim: int
    truncates: bool

    @property
    def name(self) -> str:
        return self.model.name

    @property
    def pad_from(self) -> Optional[int]:
        """Declared source dim for ``truncate_embedding``; None when
        native equals the column width (passthrough) or the legacy
        4096 path can rely on ``EMBED_NATIVE_DIM``."""
        if self.native_dim == EMBED_DIM:
            return None
        return self.native_dim


def _native_dim_of(row: ModelRegistry) -> int:
    measured = getattr(row, "embedding_native_dim", None)
    if isinstance(measured, int) and measured > 0:
        return measured
    # Pre-designation / backfilled rows without a probe: treat column
    # width as native so truncate is a no-op rather than inventing a
    # dimension. Callers that need a real probe go through set-platform-
    # embedding, which always measures.
    return EMBED_DIM


def resolve_platform_embedding(db: Session) -> Optional[PlatformEmbedding]:
    """Return the designated platform embedding, or a soft fallback.

    Preference order:
      1. ``is_platform_embedding=true`` (and active)
      2. first active ``model_type='embedding'`` row (id asc)

    Fallback exists so memory / worker keep working before an admin
    clicks "設為平台主 embedding" — the owner's standing instruction is
    not to introduce a new gate. Logging makes the missing designation
    visible without failing the turn.
    """
    designated: ModelRegistry | None = (
        db.query(ModelRegistry)
        .filter(
            ModelRegistry.is_platform_embedding.is_(True),
            ModelRegistry.model_type == "embedding",
        )
        .first()
    )
    if designated is not None:
        if not designated.is_active:
            logger.warning(
                "platform embedding %r is designated but inactive",
                designated.name,
            )
            return None
        native = _native_dim_of(designated)
        return PlatformEmbedding(
            model=designated,
            native_dim=native,
            truncates=native > EMBED_DIM,
        )

    fallback: ModelRegistry | None = (
        db.query(ModelRegistry)
        .filter(
            ModelRegistry.model_type == "embedding",
            ModelRegistry.is_active.is_(True),
        )
        .order_by(ModelRegistry.id.asc())
        .first()
    )
    if fallback is None:
        return None
    logger.warning(
        "no is_platform_embedding designation — falling back to first "
        "active embedding model %r. Admin should designate one via "
        "POST /api/models/{id}/set-platform-embedding.",
        fallback.name,
    )
    native = _native_dim_of(fallback)
    return PlatformEmbedding(
        model=fallback,
        native_dim=native,
        truncates=native > EMBED_DIM,
    )


def canonical_embedding_model_name(db: Session, name: str | None) -> str | None:
    """Return ``model_registry``'s own spelling of ``name``.

    ``model_registry.name`` is this platform's canonical name for a
    model (module docstring). A model name that arrives from anywhere
    else — an API payload, a column default, a hand-written seed — is
    free text, and free text that differs from the registry only in
    case is the shape of FAKE-CONTROLS #56: retrieval filters chunk
    provenance on the model name, so one wrong capital turns a
    perfectly-indexed corpus into one that answers nothing, with no
    error and no log line.

    Normalising here (write time) rather than at every comparison is
    what stops the defect being re-committed once per new collection.
    The case-insensitive comparisons already in the read paths stay:
    they still carry rows written before this existed, and
    ``model_registry.name`` carries no case-insensitive uniqueness
    (#56 item 8), so two spellings can still be registered side by side.

    Candidates are ``model_type='embedding'`` rows only. This column
    records which model produced a collection's vectors, so a chat model
    that happens to share the name is not a candidate — and if it were
    counted, one unrelated ``llm`` row named ``NVIDIA/NV-Embed-V2`` would
    make the real embedder look ambiguous and stop being applied.
    ``is_active`` is deliberately not filtered: a deactivated embedder is
    still the right name for the vectors it already produced (#56 item 9
    tells the operator to re-designate and if necessary reactivate
    exactly that model). ``is_active`` decides what may be *chosen*, not
    how an existing name is *spelled*.

    Returns
    -------
    * ``None`` when ``name`` is missing or blank — the caller decides
      what the default is, not this function.
    * the registry's spelling when exactly one registered embedding name
      matches case-insensitively.
    * the trimmed request otherwise. We do not guess: an unregistered
      name still fails loudly at search time ("is not registered in
      model_registry"), which beats silently binding a corpus to a row
      the caller never asked for. Two embedding registrations differing
      only in case is that same ambiguity, so it is left alone too.
    """
    if name is None:
        return None
    requested = name.strip()
    if not requested:
        return None

    from sqlalchemy import func

    spellings = {
        row[0]
        for row in db.query(ModelRegistry.name)
        .filter(
            ModelRegistry.model_type == "embedding",
            func.lower(ModelRegistry.name) == requested.lower(),
        )
        .all()
    }
    if len(spellings) == 1:
        return spellings.pop()
    if len(spellings) > 1:
        logger.warning(
            "model_registry holds %d embedding spellings of %r that differ "
            "only in case; storing the request unchanged",
            len(spellings),
            requested,
        )
    return requested


def count_pending_recompute(db: Session, designated_name: str | None) -> dict[str, int]:
    """Rows whose source model is not the current designation.

    NULL source counts as pending (legacy / pre-migration writes).

    document_chunks and ingestion_images are FORCE-RLS tables. Count them one
    collection at a time so the runtime role can set the collection GUC that
    their policies require; the explicit predicate keeps the query correct
    in SQLite tests and documents the same boundary in SQL.

    Cost measurement (2026-08-17): with 100 collections this performs 303
    ``Session.execute`` calls (2 fixed calls, 3 per collection: set the scope
    and count the two FORCE-RLS tables, plus 1 cleanup call). At the expected
    order of 300 collections that is about 903 database round trips per
    request. This is intentionally measured rather than hidden behind a cap
    because truncating the count would make this operational signal incorrect.
    """
    from sqlalchemy import text

    memory_table = "conversation_memory_chunks"
    collection_tables = ("document_chunks", "ingestion_images")
    pending_predicate = "embedding IS NOT NULL"
    if designated_name is not None:
        pending_predicate += (
            " AND (embedding_source_model IS NULL "
            "OR embedding_source_model != :name)"
        )

    out: dict[str, int] = {}
    memory_params = {"name": designated_name} if designated_name is not None else {}
    out[memory_table] = int(
        db.execute(
            text(
                f"SELECT COUNT(*) FROM {memory_table} "
                f"WHERE {pending_predicate}"
            ),
            memory_params,
        ).scalar()
        or 0
    )

    for table in collection_tables:
        out[table] = 0

    try:
        collection_ids = db.execute(
            text("SELECT id FROM ingestion_collections ORDER BY id")
        ).scalars().all()
        for collection_id in collection_ids:
            scope_collection_rls(db, int(collection_id))
            params = {"collection_id": int(collection_id)}
            if designated_name is not None:
                params["name"] = designated_name
            for table in collection_tables:
                row = db.execute(
                    text(
                        f"SELECT COUNT(*) FROM {table} "
                        f"WHERE collection_id = :collection_id "
                        f"AND {pending_predicate}"
                    ),
                    params,
                ).scalar()
                out[table] += int(row or 0)
    finally:
        bind = db.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            try:
                db.execute(
                    text("SELECT set_config('anila.collection_id', '', true)")
                )
            except Exception:
                # Tests run on SQLite; this PostgreSQL transaction-aborted
                # cleanup failure cannot be reproduced in the test database.
                # Best-effort cleanup must never mask the original exception.
                pass

    out["total"] = out[memory_table] + sum(out[t] for t in collection_tables)
    return out
