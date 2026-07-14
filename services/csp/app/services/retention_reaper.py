"""Leased, idempotent retention lifecycle for classified filesystem data."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Callable

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models.artifact import Artifact, ArtifactJob, ArtifactVersion
from app.models.ingestion import (
    IngestionCollection,
    IngestionDocument,
    IngestionDocumentGeneration,
    IngestionJob,
)
from app.models.retention import RetentionReaperLease
from app.models.source_snapshot import SourceSnapshot
from app.models.task import Task
from app.modules.artifacts.blob_store import BlobValidationError, remove_blob

logger = logging.getLogger(__name__)

_TASK_TERMINAL = frozenset({"completed", "failed", "cancelled", "blocked_by_policy"})
_ARTIFACT_JOB_TERMINAL = frozenset({"completed", "failed", "cancelled"})
_INGESTION_JOB_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "dead_letter"})
_LEASE_NAME = "classified-data-retention"


class RetentionSafetyError(RuntimeError):
    """A path or DB invariant made erasure unsafe; leave the row retryable."""


@dataclass
class RetentionRunResult:
    acquired: bool = False
    artifacts_archived: int = 0
    artifacts_erased: int = 0
    documents_archived: int = 0
    documents_erased: int = 0
    collections_erased: int = 0
    held_or_active: int = 0
    errors: list[str] = field(default_factory=list)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def safe_ingestion_path(root: str | Path, stored_path: str) -> Path:
    """Resolve an ingestion path without following symlinks or traversal."""
    base = Path(root).expanduser().resolve()
    raw = Path(stored_path)
    candidate = raw if raw.is_absolute() else base / raw
    candidate = Path(os.path.abspath(candidate))
    try:
        if os.path.commonpath((str(base), str(candidate))) != str(base):
            raise RetentionSafetyError("ingestion storage path escapes configured root")
    except ValueError as exc:
        raise RetentionSafetyError("ingestion storage path is on another volume") from exc
    if candidate == base:
        raise RetentionSafetyError("ingestion storage path cannot be the root")
    current = candidate
    while current != base:
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            mode = 0
        if mode and stat.S_ISLNK(mode):
            raise RetentionSafetyError("ingestion storage path contains a symlink")
        current = current.parent
    return candidate


def _acquire_lease(db: Session, *, token: str, now: datetime, seconds: int) -> bool:
    row = (
        db.query(RetentionReaperLease)
        .filter(RetentionReaperLease.lease_name == _LEASE_NAME)
        .with_for_update()
        .one_or_none()
    )
    if row is None:
        row = RetentionReaperLease(lease_name=_LEASE_NAME)
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            return False
    expiry = _utc(row.lease_expires_at)
    if row.lease_token and expiry and expiry > now and row.lease_token != token:
        db.rollback()
        return False
    row.lease_token = token
    row.lease_expires_at = now + timedelta(seconds=seconds)
    row.updated_at = now
    db.commit()
    return True


def _release_lease(db: Session, *, token: str, now: datetime) -> None:
    row = (
        db.query(RetentionReaperLease)
        .filter(RetentionReaperLease.lease_name == _LEASE_NAME)
        .with_for_update()
        .one_or_none()
    )
    if row is not None and row.lease_token == token:
        row.lease_token = None
        row.lease_expires_at = None
        row.updated_at = now
        db.commit()
    else:
        db.rollback()


def _artifact_has_active_work(
    db: Session, artifact: Artifact, *, lock: bool = False
) -> bool:
    if artifact.source_task_id is not None:
        query = db.query(Task).filter(Task.id == artifact.source_task_id)
        task = query.with_for_update().one_or_none() if lock else query.one_or_none()
        if task is not None and task.status not in _TASK_TERMINAL:
            return True
    if artifact.job_id:
        query = db.query(ArtifactJob).filter(ArtifactJob.job_id == artifact.job_id)
        job = query.with_for_update().one_or_none() if lock else query.one_or_none()
        if job is not None and job.status not in _ARTIFACT_JOB_TERMINAL:
            return True
    return False


def _collection_has_active_task(
    db: Session, collection_id: int, *, lock: bool = False
) -> bool:
    query = db.query(Task).filter(Task.status.notin_(_TASK_TERMINAL))
    if lock:
        query = query.with_for_update()
    tasks = query.all()
    return any(collection_id in (task.selected_collection_ids or []) for task in tasks)


def _document_has_active_work(
    db: Session, document: IngestionDocument, *, lock: bool = False
) -> bool:
    if _collection_has_active_task(db, document.collection_id, lock=lock):
        return True
    query = (
        db.query(IngestionJob)
        .filter(
            IngestionJob.document_id == document.id,
            IngestionJob.status.notin_(_INGESTION_JOB_TERMINAL),
        )
    )
    if lock:
        query = query.with_for_update()
    return query.first() is not None


def _schedule_artifact_archives(
    db: Session, *, now: datetime, active_days: int, archive_days: int, batch: int
) -> tuple[int, int]:
    changed = skipped = 0
    rows = (
        db.query(ArtifactVersion)
        .filter(ArtifactVersion.lifecycle_state == "active")
        .order_by(ArtifactVersion.id)
        .limit(batch)
        .with_for_update(skip_locked=True)
        .all()
    )
    for version in rows:
        if version.archive_due_at is None:
            version.archive_due_at = _utc(version.created_at) + timedelta(days=active_days)
        if version.legal_hold or _utc(version.archive_due_at) > now:
            continue
        artifact = db.get(Artifact, version.artifact_id)
        if artifact is None or _artifact_has_active_work(db, artifact):
            skipped += 1
            continue
        version.lifecycle_state = "archived"
        version.is_active = False
        version.archived_at = now
        version.erase_due_at = now + timedelta(days=archive_days)
        changed += 1
    db.commit()
    return changed, skipped


def _schedule_document_archives(
    db: Session, *, now: datetime, active_days: int, archive_days: int, batch: int
) -> tuple[int, int]:
    changed = skipped = 0
    rows = (
        db.query(IngestionDocument)
        .filter(IngestionDocument.lifecycle_state == "active")
        .order_by(IngestionDocument.id)
        .limit(batch)
        .with_for_update(skip_locked=True)
        .all()
    )
    for document in rows:
        if document.archive_due_at is None:
            document.archive_due_at = _utc(document.uploaded_at) + timedelta(days=active_days)
        collection = db.get(IngestionCollection, document.collection_id)
        if (
            document.legal_hold
            or (collection is not None and collection.legal_hold)
            or _utc(document.archive_due_at) > now
        ):
            continue
        if _document_has_active_work(db, document):
            skipped += 1
            continue
        document.lifecycle_state = "archived"
        document.archived_at = now
        document.erase_due_at = now + timedelta(days=archive_days)
        document.availability_status = "unavailable"
        changed += 1
    db.commit()
    return changed, skipped


def _snapshot_collection_ids(db: Session, artifact: Artifact) -> set[int]:
    if artifact.source_snapshot_id is None:
        return set()
    snapshot = db.get(SourceSnapshot, artifact.source_snapshot_id)
    try:
        return {int(value) for value in ((snapshot.collection_ids if snapshot else []) or [])}
    except (TypeError, ValueError):
        return set()


def reconcile_collection_counters(db: Session, collection_id: int) -> None:
    """Recompute, never decrement, so retries cannot create negative counters."""
    collection = db.get(IngestionCollection, collection_id)
    if collection is None:
        return
    documents = (
        db.query(IngestionDocument)
        .filter(
            IngestionDocument.collection_id == collection_id,
            IngestionDocument.lifecycle_state != "erased",
        )
        .all()
    )
    collection.document_count = len(documents)
    collection.chunk_count = max(0, sum(max(0, doc.chunk_count or 0) for doc in documents))
    collection.bytes_stored = max(0, sum(max(0, doc.bytes or 0) for doc in documents))
    tables = set(inspect(db.connection()).get_table_names())
    if "ingestion_images" in tables:
        collection.image_count = int(db.execute(text(
            "SELECT count(*) FROM ingestion_images WHERE collection_id=:cid"
        ), {"cid": collection_id}).scalar() or 0)
    collection.artifact_count = int(
        db.query(Artifact.id)
        .join(ArtifactJob, ArtifactJob.job_id == Artifact.job_id)
        .filter(
            ArtifactJob.collection_id == collection_id,
            Artifact.status != "erased",
        )
        .count()
    )
    db.flush()


def _erase_artifact_version(
    db: Session, *, version_id: int, now: datetime, artifact_root: str,
    after_marker: Callable[[Session], None] | None = None,
) -> bool:
    version = (
        db.query(ArtifactVersion)
        .filter(ArtifactVersion.id == version_id)
        .with_for_update()
        .one_or_none()
    )
    if version is None or version.lifecycle_state == "erased":
        db.rollback()
        return False
    artifact = db.get(Artifact, version.artifact_id)
    if (
        artifact is None
        or version.legal_hold
        or _utc(version.erase_due_at) is None
        or _utc(version.erase_due_at) > now
        or _artifact_has_active_work(db, artifact)
    ):
        db.rollback()
        return False
    if version.lifecycle_state not in {"archived", "revoked", "erase_due"}:
        db.rollback()
        return False
    version.lifecycle_state = "erase_due"
    version.is_active = False
    db.commit()
    if after_marker is not None:
        after_marker(db)

    # Crash marker is durable. Reacquire and re-evaluate every guard before
    # unlink, then hold this row lock across filesystem erasure and metadata
    # commit so a concurrent legal-hold mutation cannot win the TOCTOU window.
    version = (
        db.query(ArtifactVersion)
        .filter(ArtifactVersion.id == version_id)
        .with_for_update()
        .one_or_none()
    )
    if version is None or version.lifecycle_state == "erased":
        db.rollback()
        return False
    artifact = db.query(Artifact).filter(
        Artifact.id == version.artifact_id
    ).with_for_update().one_or_none()
    if (
        artifact is None
        or version.legal_hold
        or version.lifecycle_state != "erase_due"
        or _utc(version.erase_due_at) is None
        or _utc(version.erase_due_at) > now
        or _artifact_has_active_work(db, artifact, lock=True)
    ):
        db.rollback()
        return False
    blob_key = version.blob_key
    if blob_key:
        try:
            remove_blob(artifact_root, blob_key)
        except BlobValidationError as exc:
            raise RetentionSafetyError(str(exc)) from exc

    version.blob_key = None
    version.blob_size_bytes = None
    version.media_type = None
    version.original_filename = None
    version.storage_ref = None
    version.file_refs = []
    version.lifecycle_state = "erased"
    version.erased_at = now
    version.is_active = False
    db.flush()
    active = db.query(ArtifactVersion).filter(
        ArtifactVersion.artifact_id == artifact.id,
        ArtifactVersion.lifecycle_state == "active",
    ).count()
    erased = db.query(ArtifactVersion).filter(
        ArtifactVersion.artifact_id == artifact.id,
        ArtifactVersion.lifecycle_state == "erased",
    ).count()
    remaining = db.query(ArtifactVersion).filter(
        ArtifactVersion.artifact_id == artifact.id,
        ArtifactVersion.lifecycle_state != "erased",
    ).count()
    artifact.active_version_count = max(0, active)
    artifact.erased_version_count = max(0, erased)
    collection_ids = _snapshot_collection_ids(db, artifact)
    if remaining == 0:
        artifact.status = "erased"
    for collection_id in collection_ids:
        reconcile_collection_counters(db, collection_id)
    db.commit()
    return True


def _image_rows(db: Session, document_id: int) -> list[tuple[int, str]]:
    if "ingestion_images" not in set(inspect(db.connection()).get_table_names()):
        return []
    return [
        (int(row.id), str(row.storage_path))
        for row in db.execute(text(
            "SELECT id, storage_path FROM ingestion_images WHERE document_id=:did"
        ), {"did": document_id}).all()
    ]


def _erase_document(
    db: Session, *, document_id: int, now: datetime, ingestion_root: str,
    after_marker: Callable[[Session], None] | None = None,
) -> bool:
    document = (
        db.query(IngestionDocument)
        .filter(IngestionDocument.id == document_id)
        .with_for_update()
        .one_or_none()
    )
    if document is None or document.lifecycle_state == "erased":
        db.rollback()
        return False
    collection = db.get(IngestionCollection, document.collection_id)
    if (
        document.legal_hold
        or (collection is not None and collection.legal_hold)
        or _utc(document.erase_due_at) is None
        or _utc(document.erase_due_at) > now
        or _document_has_active_work(db, document)
    ):
        db.rollback()
        return False
    if document.lifecycle_state not in {"archived", "erase_due"}:
        db.rollback()
        return False
    document.lifecycle_state = "erase_due"
    document.availability_status = "unavailable"
    db.commit()
    if after_marker is not None:
        after_marker(db)

    # Same two-phase pattern as artifacts: marker first, then reacquire all
    # policy rows and hold their locks until bytes and DB refs converge.
    document = (
        db.query(IngestionDocument)
        .filter(IngestionDocument.id == document_id)
        .with_for_update()
        .one_or_none()
    )
    if document is None or document.lifecycle_state == "erased":
        db.rollback()
        return False
    collection = db.query(IngestionCollection).filter(
        IngestionCollection.id == document.collection_id
    ).with_for_update().one_or_none()
    if (
        document.legal_hold
        or (collection is not None and collection.legal_hold)
        or document.lifecycle_state != "erase_due"
        or _utc(document.erase_due_at) is None
        or _utc(document.erase_due_at) > now
        or _document_has_active_work(db, document, lock=True)
    ):
        db.rollback()
        return False
    images = _image_rows(db, document.id)
    image_paths = [safe_ingestion_path(ingestion_root, path) for _id, path in images]
    doc_path = (
        safe_ingestion_path(ingestion_root, document.storage_path)
        if document.storage_path else None
    )
    for path in image_paths:
        path.unlink(missing_ok=True)
    shared = db.query(IngestionDocument.id).filter(
        IngestionDocument.sha256 == document.sha256,
        IngestionDocument.id != document.id,
        IngestionDocument.lifecycle_state != "erased",
    ).first()
    if doc_path is not None and shared is None:
        doc_path.unlink(missing_ok=True)

    tables = set(inspect(db.connection()).get_table_names())
    if "ingestion_images" in tables:
        db.execute(text("DELETE FROM ingestion_images WHERE document_id=:did"),
                   {"did": document.id})
    if "document_chunks" in tables:
        db.execute(text("DELETE FROM document_chunks WHERE document_id=:did"),
                   {"did": document.id})
    if "document_relations" in tables:
        db.execute(text(
            "DELETE FROM document_relations "
            "WHERE src_document_id=:did OR dst_document_id=:did"
        ), {"did": document.id})
    document.active_generation_id = None
    db.flush()
    db.query(IngestionDocumentGeneration).filter(
        IngestionDocumentGeneration.document_id == document.id
    ).delete(synchronize_session=False)
    document.storage_path = None
    document.bytes = 0
    document.chunk_count = 0
    document.status = "erased"
    document.processing_stage = "complete"
    document.availability_status = "unavailable"
    document.lifecycle_state = "erased"
    document.erased_at = now
    reconcile_collection_counters(db, document.collection_id)
    db.commit()
    return True


def _erase_due_artifacts(
    db: Session, *, now: datetime, batch: int, artifact_root: str
) -> tuple[int, list[str]]:
    ids = [row[0] for row in db.query(ArtifactVersion.id).filter(
        ArtifactVersion.lifecycle_state.in_(("archived", "revoked", "erase_due")),
        ArtifactVersion.erase_due_at.is_not(None),
        ArtifactVersion.erase_due_at <= now,
        ArtifactVersion.legal_hold.is_(False),
    ).order_by(ArtifactVersion.id).limit(batch).all()]
    erased = 0
    errors: list[str] = []
    for version_id in ids:
        try:
            erased += int(_erase_artifact_version(
                db, version_id=version_id, now=now, artifact_root=artifact_root
            ))
        except Exception as exc:  # noqa: BLE001 - keep other rows progressing
            db.rollback()
            errors.append(f"artifact_version:{version_id}:{exc}")
    return erased, errors


def _erase_due_documents(
    db: Session, *, now: datetime, batch: int, ingestion_root: str
) -> tuple[int, list[str]]:
    ids = [row[0] for row in db.query(IngestionDocument.id).filter(
        IngestionDocument.lifecycle_state.in_(("archived", "erase_due")),
        IngestionDocument.erase_due_at.is_not(None),
        IngestionDocument.erase_due_at <= now,
        IngestionDocument.legal_hold.is_(False),
    ).order_by(IngestionDocument.id).limit(batch).all()]
    erased = 0
    errors: list[str] = []
    for document_id in ids:
        try:
            erased += int(_erase_document(
                db, document_id=document_id, now=now, ingestion_root=ingestion_root
            ))
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            errors.append(f"document:{document_id}:{exc}")
    return erased, errors


def _erase_empty_collections(db: Session, *, now: datetime, batch: int) -> int:
    rows = db.query(IngestionCollection).filter(
        IngestionCollection.lifecycle_state.in_(("archived", "erase_due")),
        IngestionCollection.erase_due_at.is_not(None),
        IngestionCollection.erase_due_at <= now,
        IngestionCollection.legal_hold.is_(False),
    ).order_by(IngestionCollection.id).limit(batch).with_for_update(skip_locked=True).all()
    erased = 0
    for collection in rows:
        if _collection_has_active_task(db, collection.id):
            continue
        active_job = db.query(IngestionJob.id).filter(
            IngestionJob.collection_id == collection.id,
            IngestionJob.status.notin_(_INGESTION_JOB_TERMINAL),
        ).first()
        remaining = db.query(IngestionDocument.id).filter(
            IngestionDocument.collection_id == collection.id,
            IngestionDocument.lifecycle_state != "erased",
        ).first()
        if active_job is not None or remaining is not None:
            continue
        collection.lifecycle_state = "erased"
        collection.erased_at = now
        collection.status = "archived"
        collection.document_count = 0
        collection.chunk_count = 0
        collection.bytes_stored = 0
        collection.image_count = 0
        collection.artifact_count = max(0, collection.artifact_count or 0)
        erased += 1
    db.commit()
    return erased


def run_retention_batch(
    db: Session,
    *,
    now: datetime | None = None,
    token: str | None = None,
    artifact_root: str | None = None,
    ingestion_root: str | None = None,
    batch_size: int | None = None,
) -> RetentionRunResult:
    now = _utc(now or datetime.now(timezone.utc))
    token = token or uuid.uuid4().hex
    result = RetentionRunResult()
    if not settings.RETENTION_ENABLED:
        return result
    result.acquired = _acquire_lease(
        db, token=token, now=now, seconds=settings.RETENTION_REAPER_LEASE_SECONDS
    )
    if not result.acquired:
        return result
    batch = batch_size or settings.RETENTION_REAPER_BATCH_SIZE
    try:
        archived, skipped = _schedule_artifact_archives(
            db, now=now,
            active_days=settings.RETENTION_ARTIFACT_ACTIVE_DAYS,
            archive_days=settings.RETENTION_ARTIFACT_ARCHIVE_DAYS,
            batch=batch,
        )
        result.artifacts_archived += archived
        result.held_or_active += skipped
        archived, skipped = _schedule_document_archives(
            db, now=now,
            active_days=settings.RETENTION_INGESTION_ACTIVE_DAYS,
            archive_days=settings.RETENTION_INGESTION_ARCHIVE_DAYS,
            batch=batch,
        )
        result.documents_archived += archived
        result.held_or_active += skipped
        result.artifacts_erased, errors = _erase_due_artifacts(
            db, now=now, batch=batch,
            artifact_root=artifact_root or settings.ARTIFACT_BLOB_STORAGE_PATH,
        )
        result.errors.extend(errors)
        result.documents_erased, errors = _erase_due_documents(
            db, now=now, batch=batch,
            ingestion_root=ingestion_root or settings.INGESTION_UPLOAD_DIR,
        )
        result.errors.extend(errors)
        result.collections_erased = _erase_empty_collections(db, now=now, batch=batch)
    finally:
        try:
            _release_lease(db, token=token, now=now)
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("retention lease release failed; expiry will recover it")
    return result


async def _retention_loop() -> None:
    while True:
        try:
            def _run() -> RetentionRunResult:
                db = SessionLocal()
                try:
                    return run_retention_batch(db)
                finally:
                    db.close()

            result = await asyncio.to_thread(_run)
            if result.errors:
                logger.error("retention reaper partial failures: %s", result.errors)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("retention reaper cycle failed")
        await asyncio.sleep(settings.RETENTION_REAPER_INTERVAL_SECONDS)


async def start_retention_reaper() -> asyncio.Task | None:
    if not settings.RETENTION_ENABLED:
        return None
    return asyncio.create_task(_retention_loop(), name="retention-reaper")


__all__ = [
    "RetentionRunResult",
    "RetentionSafetyError",
    "reconcile_collection_counters",
    "run_retention_batch",
    "safe_ingestion_path",
    "start_retention_reaper",
]
