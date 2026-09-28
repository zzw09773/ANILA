"""Console designation starts a rebuild. Search keeps the previous model until it finishes."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from anila_core.embeddings.dims import ann_index_sql
from anila_core.embeddings.swap import (
    OLD_VECTOR_RETENTION_DAYS,
    ActivationState,
    cleanup_model_id,
    eta_seconds,
    plan_designation,
    rollback_state,
)

from app.models.embedding_activation import EmbeddingActivation
from app.models.ingestion import IngestionCollection
from app.models.model_registry import ModelRegistry

logger = logging.getLogger(__name__)

_OPEN = frozenset({"pending", "running"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _row(db: Session) -> EmbeddingActivation | None:
    return db.query(EmbeddingActivation).filter(EmbeddingActivation.id == 1).one_or_none()


def _ensure(db: Session) -> EmbeddingActivation:
    row = _row(db)
    if row is None:
        row = EmbeddingActivation(id=1, rebuild_done=0, rebuild_total=0, rebuild_errors=0)
        db.add(row)
        db.flush()
    return row


def _to_state(row: EmbeddingActivation) -> ActivationState:
    return ActivationState(
        active_model_id=row.active_model_id,
        previous_model_id=row.previous_model_id,
        switched_at=_aware(row.switched_at),
        rebuild_target_model_id=row.rebuild_target_model_id,
        rebuild_status=row.rebuild_status,
        rebuild_done=int(row.rebuild_done or 0),
        rebuild_total=int(row.rebuild_total or 0),
        rebuild_errors=int(row.rebuild_errors or 0),
        rebuild_started_at=_aware(row.rebuild_started_at),
    )


def _apply(row: EmbeddingActivation, state: ActivationState, *, now: datetime) -> None:
    row.active_model_id = state.active_model_id
    row.previous_model_id = state.previous_model_id
    row.switched_at = state.switched_at
    row.rebuild_target_model_id = state.rebuild_target_model_id
    row.rebuild_status = state.rebuild_status
    row.rebuild_done = state.rebuild_done
    row.rebuild_total = state.rebuild_total
    row.rebuild_errors = state.rebuild_errors
    row.rebuild_started_at = state.rebuild_started_at
    row.touch(now)


def _model_by_name(db: Session, name: str | None) -> ModelRegistry | None:
    if not name or not name.strip():
        return None
    return (
        db.query(ModelRegistry)
        .filter(
            ModelRegistry.model_type == "embedding",
            func.lower(ModelRegistry.name) == name.strip().lower(),
        )
        .order_by(ModelRegistry.is_active.desc(), ModelRegistry.id.asc())
        .first()
    )


def _corpus_model_name(db: Session, target_name: str) -> str | None:
    rows = (
        db.query(IngestionCollection.embedding_model)
        .filter(
            IngestionCollection.status == "active",
            IngestionCollection.embedding_model.isnot(None),
        )
        .all()
    )
    for (name,) in rows:
        if name and name.lower() != target_name.lower():
            return name
    return None


def _set_role_flag(db: Session, model_id: int) -> None:
    (
        db.query(ModelRegistry)
        .filter(
            ModelRegistry.is_platform_embedding.is_(True),
            ModelRegistry.id != model_id,
        )
        .update({"is_platform_embedding": False}, synchronize_session=False)
    )
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).one_or_none()
    if model is not None:
        model.is_platform_embedding = True


def note_designation(
    db: Session,
    *,
    target_id: int,
    target_name: str,
    previous_name: str | None,
) -> dict[str, Any]:
    """Record the console choice without moving the model search uses."""
    now = _now()
    row = _ensure(db)
    keep_id = None
    corpus_name = _corpus_model_name(db, target_name)
    corpus = _model_by_name(db, corpus_name)
    if corpus is not None and corpus.id != target_id:
        keep_id = corpus.id
    elif previous_name and previous_name.lower() != target_name.lower():
        previous = _model_by_name(db, previous_name)
        if previous is not None and previous.id != target_id:
            keep_id = previous.id
    state = plan_designation(
        _to_state(row),
        target_model_id=target_id,
        keep_model_id=keep_id,
        now=now,
    )
    _apply(row, state, now=now)
    db.flush()
    return {
        "rebuild_started": state.rebuild_status in _OPEN,
        "active_model_id": state.active_model_id,
        "rebuild_target_model_id": state.rebuild_target_model_id,
    }


def search_model(db: Session) -> ModelRegistry | None:
    """The model retrieval must embed and filter with.

    Falls back to the console role when no activation row has been
    written yet (tests and a database that has not been designated).
    """
    row = _row(db)
    model_id = row.active_model_id if row is not None else None
    if model_id is not None:
        model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).one_or_none()
        if model is not None:
            return model
    return (
        db.query(ModelRegistry)
        .filter(
            ModelRegistry.is_platform_embedding.is_(True),
            ModelRegistry.model_type == "embedding",
            ModelRegistry.is_active.is_(True),
        )
        .first()
    )


def rebuild_open(db: Session) -> bool:
    row = _row(db)
    return row is not None and row.rebuild_status in _OPEN and row.rebuild_target_model_id is not None


def write_model_ids(db: Session) -> list[int]:
    """Models a newly written row must carry vectors for."""
    row = _row(db)
    role = (
        db.query(ModelRegistry)
        .filter(
            ModelRegistry.is_platform_embedding.is_(True),
            ModelRegistry.model_type == "embedding",
            ModelRegistry.is_active.is_(True),
        )
        .first()
    )
    ids: list[int] = []
    if row is not None and row.active_model_id and rebuild_open(db):
        ids.append(row.active_model_id)
    if role is not None and role.id not in ids:
        ids.append(role.id)
    elif row is not None and row.active_model_id and row.active_model_id not in ids:
        ids.append(row.active_model_id)
    return ids


def column_model_id(db: Session) -> int | None:
    """The vector stored on the legacy column: the one search still uses."""
    row = _row(db)
    if row is not None and row.active_model_id:
        return row.active_model_id
    ids = write_model_ids(db)
    return ids[0] if ids else None


def _vectors_remain(db: Session, model_id: int | None) -> bool:
    if model_id is None:
        return False
    if db.get_bind().dialect.name != "postgresql":
        return True
    try:
        found = db.execute(
            text(
                "SELECT 1 FROM embedding_vectors WHERE model_id = :model_id LIMIT 1"
            ),
            {"model_id": model_id},
        ).first()
    except Exception:
        logger.exception("embedding_swap: could not check leftover vectors")
        return True
    return found is not None


def snapshot(db: Session, *, now: datetime | None = None) -> dict[str, Any]:
    moment = now or _now()
    row = _row(db)
    if row is None:
        return {
            "active_model_id": None,
            "active_model_name": None,
            "previous_model_id": None,
            "previous_model_name": None,
            "rollback_available": False,
            "rebuild": None,
        }
    state = _to_state(row)
    active = (
        db.query(ModelRegistry).filter(ModelRegistry.id == row.active_model_id).one_or_none()
        if row.active_model_id
        else None
    )
    previous = (
        db.query(ModelRegistry).filter(ModelRegistry.id == row.previous_model_id).one_or_none()
        if row.previous_model_id
        else None
    )
    target = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.id == row.rebuild_target_model_id)
        .one_or_none()
        if row.rebuild_target_model_id
        else None
    )
    rollback_available = False
    if state.rebuild_status in _OPEN and state.active_model_id is not None:
        rollback_available = True
    elif state.previous_model_id and _vectors_remain(db, state.previous_model_id):
        rollback_available = state.previous_model_id != state.active_model_id
    rebuild = None
    show_rebuild = state.rebuild_status in _OPEN or state.rebuild_errors > 0 or (
        state.rebuild_status in {"failed", "cancelled"}
    )
    if show_rebuild:
        rebuild = {
            "status": state.rebuild_status,
            "target_model_id": state.rebuild_target_model_id,
            "target_model_name": target.name if target is not None else None,
            "done": state.rebuild_done,
            "total": state.rebuild_total,
            "errors": state.rebuild_errors,
            "eta_seconds": eta_seconds(
                done=state.rebuild_done,
                total=state.rebuild_total,
                started_at=state.rebuild_started_at,
                now=moment,
            ),
            "last_error": row.rebuild_last_error,
        }
    return {
        "active_model_id": state.active_model_id,
        "active_model_name": active.name if active is not None else None,
        "previous_model_id": state.previous_model_id,
        "previous_model_name": previous.name if previous is not None else None,
        "rollback_available": rollback_available,
        "rebuild": rebuild,
    }


def retry_unembeddable(db: Session) -> dict[str, Any]:
    """清掉三次失敗的記號，讓那些列回到可嵌入集合，並重新排隊。"""
    now = _now()
    row = _ensure(db)
    model_id = row.rebuild_target_model_id or row.active_model_id
    if model_id is None:
        raise ValueError("沒有可重試的嵌入模型")
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text(
                """
                DELETE FROM embedding_rebuild_failures
                 WHERE model_id = :model_id
                   AND attempts >= 3
                """
            ),
            {"model_id": model_id},
        )
    if row.rebuild_status not in _OPEN:
        row.rebuild_status = "pending"
        row.rebuild_target_model_id = model_id
        row.rebuild_started_at = now
        row.rebuild_last_error = None
    row.rebuild_errors = 0
    row.touch(now)
    db.flush()
    return snapshot(db, now=now)


def rollback(db: Session) -> dict[str, Any]:
    now = _now()
    row = _ensure(db)
    state = _to_state(row)
    vectors_exist = _vectors_remain(db, state.previous_model_id)
    if state.rebuild_status in _OPEN:
        vectors_exist = True
    new_state = rollback_state(state, now=now, previous_vectors_exist=vectors_exist)
    _apply(row, new_state, now=now)
    if new_state.active_model_id is not None:
        _set_role_flag(db, new_state.active_model_id)
    db.flush()
    return snapshot(db, now=now)


def purge_expired(db: Session, *, now: datetime | None = None) -> int | None:
    """Delete the previous model's vectors once the retention window has passed.

    Returns the model id that was deleted, or None.
    """
    moment = now or _now()
    row = _row(db)
    if row is None:
        return None
    state = _to_state(row)
    model_id = cleanup_model_id(state, now=moment)
    if model_id is None:
        return None
    _delete_model_vectors(db, model_id)
    row.previous_model_id = None
    row.touch(moment)
    db.flush()
    return model_id


def _delete_model_vectors(db: Session, model_id: int) -> None:
    if db.get_bind().dialect.name != "postgresql":
        return
    db.execute(
        text(
            """
            DELETE FROM embedding_vectors
             WHERE model_id = :model_id
               AND subject <> 'chunk'
            """
        ),
        {"model_id": model_id},
    )
    db.execute(
        text(
            """
            DELETE FROM embedding_rebuild_failures
             WHERE model_id = :model_id
            """
        ),
        {"model_id": model_id},
    )
    collection_ids = [
        int(item[0])
        for item in db.execute(text("SELECT id FROM ingestion_collections")).fetchall()
    ]
    for collection_id in collection_ids:
        db.execute(
            text("SELECT set_config('anila.collection_id', :cid, true)"),
            {"cid": str(collection_id)},
        )
        db.execute(
            text(
                """
                DELETE FROM embedding_vectors
                 WHERE model_id = :model_id
                   AND subject = 'chunk'
                   AND collection_id = :collection_id
                """
            ),
            {"model_id": model_id, "collection_id": collection_id},
        )
    try:
        names = db.execute(
            text(
                """
                SELECT indexname
                  FROM pg_indexes
                 WHERE tablename = 'embedding_vectors'
                   AND indexname LIKE :prefix
                """
            ),
            {"prefix": f"ix_embvec_hnsw_%_{model_id}_%"},
        ).fetchall()
    except Exception:
        names = []
    for (index_name,) in names:
        if index_name.startswith("ix_embvec_hnsw_") and str(model_id) in index_name.split("_"):
            db.execute(text(f'DROP INDEX IF EXISTS "{index_name}"'))


def upsert_slot(
    db: Session,
    *,
    subject: str,
    subject_id: int,
    model_id: int,
    vector: list[float],
    native_dims: int,
    collection_id: int | None = None,
) -> None:
    """Best-effort side-table write. Missing table (pre-migration) is ignored."""
    if db.get_bind().dialect.name != "postgresql" or not vector:
        return
    from anila_core.embeddings.dims import fit_stored_vector

    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).one_or_none()
    declared = None
    if model is not None and isinstance(model.embedding_native_dim, int) and model.embedding_native_dim > 0:
        declared = model.embedding_native_dim
    try:
        stored = fit_stored_vector(vector, declared_native=declared)
    except ValueError:
        stored = fit_stored_vector(vector, declared_native=None)
    literal = "[" + ",".join(str(float(x)) for x in stored.values) + "]"
    params = {
        "subject": subject,
        "subject_id": subject_id,
        "model_id": model_id,
        "collection_id": collection_id,
        "dims": stored.dims,
        "native_dims": native_dims or stored.native_dims,
        "vec": literal,
    }
    try:
        # Savepoints: a failed CREATE INDEX must not abort the caller's
        # transaction, and must not roll back the vector row either.
        with db.begin_nested():
            if subject == "chunk" and collection_id is not None:
                db.execute(
                    text("SELECT set_config('anila.collection_id', :cid, true)"),
                    {"cid": str(int(collection_id))},
                )
            db.execute(
                text(
                    """
                    INSERT INTO embedding_vectors
                        (subject, subject_id, model_id, collection_id, dims, native_dims, embedding)
                    VALUES
                        (:subject, :subject_id, :model_id, :collection_id, :dims, :native_dims,
                         CAST(:vec AS halfvec))
                    ON CONFLICT (subject, subject_id, model_id) DO UPDATE SET
                        dims = EXCLUDED.dims,
                        native_dims = EXCLUDED.native_dims,
                        embedding = EXCLUDED.embedding,
                        collection_id = EXCLUDED.collection_id
                    """
                ),
                params,
            )
        if 1 <= stored.dims <= 4000:
            try:
                with db.begin_nested():
                    db.execute(
                        text(ann_index_sql(model_id=model_id, dims=stored.dims, subject=subject))
                    )
            except Exception:
                logger.warning(
                    "embedding index skipped model=%s dims=%s",
                    model_id,
                    stored.dims,
                    exc_info=True,
                )
    except Exception:
        logger.warning(
            "embedding slot write skipped subject=%s id=%s model=%s",
            subject,
            subject_id,
            model_id,
            exc_info=True,
        )


def dispatch_enabled() -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    return True


async def enqueue_rebuild() -> None:
    if not dispatch_enabled():
        return
    import asyncio

    from app.services.ingestion_queue import enqueue_embedding_rebuild

    await asyncio.wait_for(enqueue_embedding_rebuild(), timeout=2.0)


def start_embedding_cleanup():
    """Daily deletion of vectors retired more than seven days ago."""
    import asyncio

    async def _loop() -> None:
        await asyncio.sleep(0)
        while True:
            try:
                await asyncio.to_thread(_purge_once)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("embedding cleanup failed")
            try:
                await asyncio.sleep(_seconds_until_next_utc_midnight(_now()))
            except asyncio.CancelledError:
                raise

    return asyncio.create_task(_loop())


def _purge_once() -> None:
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        purged = purge_expired(db)
        db.commit()
        if purged is not None:
            logger.info("embedding cleanup deleted model_id=%s", purged)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _seconds_until_next_utc_midnight(now: datetime) -> float:
    aware = _aware(now) or _now()
    nxt = (aware + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(60.0, (nxt - aware).total_seconds())
