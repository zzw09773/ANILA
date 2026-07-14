from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.models.artifact import Artifact, ArtifactJob, ArtifactVersion
from app.models.ingestion import IngestionCollection, IngestionDocument, IngestionJob
from app.models.retention import RetentionReaperLease
from app.models.source_snapshot import SourceSnapshot
from app.models.task import Task
from app.modules import tasks as tasks_module
from app.schemas.contracts.tasks import TaskCreate
from app.services import retention_reaper
from app.services.retention_reaper import (
    RetentionSafetyError,
    run_retention_batch,
    safe_ingestion_path,
)
from tests.conftest import make_user


NOW = datetime(2035, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def _collection(db, owner, *, name="retention") -> IngestionCollection:
    row = IngestionCollection(
        name=name,
        chunking_config={},
        embedding_model="test",
        embedding_fingerprint=f"sha256:{'0' * 64}",
        embedding_dim=3,
        created_by=owner.id,
        classification_level="機密",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _artifact_due(db, tmp_path, *, task_status="completed"):
    owner = make_user(db, username=f"retention_artifact_{task_status}")
    collection = _collection(db, owner, name=f"artifact-{task_status}")
    task = Task(
        title="retention",
        task_type="generate_artifact",
        requester_user_id=owner.id,
        status=task_status,
        source_scope="project",
        selected_collection_ids=[collection.id],
        classification_level="機密",
    )
    db.add(task)
    db.flush()
    snapshot = SourceSnapshot(
        task_id=task.id,
        origin="collection",
        source_scope="project",
        collection_ids=[collection.id],
        classification_level="機密",
    )
    db.add(snapshot)
    db.flush()
    task.source_snapshot_id = snapshot.id
    job = ArtifactJob(
        job_id=f"retention-{task_status}",
        owner_user_id=owner.id,
        collection_id=collection.id,
        task_id=task.id,
        source_snapshot_id=snapshot.id,
        artifact_type="report",
        status="completed",
    )
    db.add(job)
    db.flush()
    artifact = Artifact(
        artifact_type="report",
        title="retention report",
        status="completed",
        owner_user_id=owner.id,
        source_task_id=task.id,
        source_snapshot_id=snapshot.id,
        job_id=job.job_id,
        classification_level="機密",
        active_version_count=0,
    )
    db.add(artifact)
    db.flush()
    job.artifact_id = artifact.id
    content = b"retention artifact"
    digest = hashlib.sha256(content).hexdigest()
    key = f"{digest[:2]}/{'a' * 64}.pdf"
    path = tmp_path / key
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    version = ArtifactVersion(
        artifact_id=artifact.id,
        version=1,
        content_hash=digest,
        blob_key=key,
        blob_size_bytes=len(content),
        media_type="application/pdf",
        original_filename="report.pdf",
        is_active=False,
        lifecycle_state="archived",
        archived_at=NOW - timedelta(days=3),
        erase_due_at=NOW,
        classification_level="機密",
    )
    db.add(version)
    db.commit()
    return owner, collection, task, artifact, version, path


def _raw_ingestion_tables(db) -> None:
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS ingestion_images (
          id INTEGER PRIMARY KEY,
          collection_id INTEGER NOT NULL,
          document_id INTEGER NOT NULL,
          storage_path TEXT NOT NULL
        )
    """))
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS document_chunks (
          id INTEGER PRIMARY KEY,
          collection_id INTEGER NOT NULL,
          document_id INTEGER NOT NULL
        )
    """))
    db.execute(text("DELETE FROM ingestion_images"))
    db.execute(text("DELETE FROM document_chunks"))
    db.commit()


def _document_due(db, tmp_path, *, image_path="anila-images/1/img.png"):
    _raw_ingestion_tables(db)
    owner = make_user(db, username=f"retention_doc_{abs(hash(image_path))}")
    collection = _collection(db, owner, name=f"doc-{abs(hash(image_path))}")
    blob = tmp_path / "ab" / ("b" * 64)
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(b"document")
    document = IngestionDocument(
        collection_id=collection.id,
        filename="doc.pdf",
        sha256="b" * 64,
        mime_type="application/pdf",
        bytes=8,
        storage_path=str(blob),
        status="indexed",
        chunk_count=2,
        availability_status="unavailable",
        processing_stage="complete",
        uploaded_by=owner.id,
        classification_level="機密",
        lifecycle_state="archived",
        archived_at=NOW - timedelta(days=2),
        erase_due_at=NOW,
    )
    db.add(document)
    db.flush()
    resolved_image = tmp_path / image_path
    if ".." not in image_path:
        resolved_image.parent.mkdir(parents=True, exist_ok=True)
        resolved_image.write_bytes(b"image")
    db.execute(text(
        "INSERT INTO ingestion_images(id,collection_id,document_id,storage_path) "
        "VALUES (1,:cid,:did,:path)"
    ), {"cid": collection.id, "did": document.id, "path": image_path})
    db.execute(text(
        "INSERT INTO document_chunks(id,collection_id,document_id) "
        "VALUES (1,:cid,:did),(2,:cid,:did)"
    ), {"cid": collection.id, "did": document.id})
    collection.document_count = 1
    collection.chunk_count = 2
    collection.bytes_stored = 8
    collection.image_count = 1
    db.commit()
    return owner, collection, document, blob, resolved_image


def test_artifact_boundary_erase_is_idempotent_and_keeps_hash(db, tmp_path):
    _owner, collection, _task, artifact, version, path = _artifact_due(db, tmp_path)
    first = run_retention_batch(
        db, now=NOW, artifact_root=str(tmp_path), ingestion_root=str(tmp_path)
    )
    assert first.acquired and first.artifacts_erased == 1
    assert not path.exists()
    db.refresh(version)
    db.refresh(artifact)
    db.refresh(collection)
    assert version.lifecycle_state == "erased"
    assert version.content_hash is not None
    assert version.blob_key is version.blob_size_bytes is version.media_type is None
    assert artifact.status == "erased"
    assert artifact.active_version_count == 0
    assert artifact.erased_version_count == 1
    assert collection.artifact_count == 0

    second = run_retention_batch(
        db, now=NOW, artifact_root=str(tmp_path), ingestion_root=str(tmp_path)
    )
    assert second.acquired and second.artifacts_erased == 0


def test_legal_hold_and_active_task_prevent_artifact_erase(db, tmp_path):
    _owner, _collection, _task, _artifact, version, path = _artifact_due(db, tmp_path)
    version.legal_hold = True
    version.legal_hold_reason = "CASE-123"
    db.commit()
    result = run_retention_batch(
        db, now=NOW, artifact_root=str(tmp_path), ingestion_root=str(tmp_path)
    )
    assert result.artifacts_erased == 0 and path.exists()

    version.legal_hold = False
    version.legal_hold_reason = None
    task = db.get(Task, version.artifact.source_task_id)
    task.status = "running"
    db.commit()
    result = run_retention_batch(
        db, now=NOW, artifact_root=str(tmp_path), ingestion_root=str(tmp_path)
    )
    assert result.artifacts_erased == 0 and path.exists()


def test_hold_created_after_erase_due_marker_wins_before_unlink(db, tmp_path):
    _owner, _collection, _task, _artifact, version, path = _artifact_due(db, tmp_path)

    def establish_hold(session):
        row = session.get(ArtifactVersion, version.id)
        row.legal_hold = True
        row.legal_hold_reason = "RACE-HOLD"
        session.commit()

    erased = retention_reaper._erase_artifact_version(
        db,
        version_id=version.id,
        now=NOW,
        artifact_root=str(tmp_path),
        after_marker=establish_hold,
    )
    assert not erased
    assert path.exists()
    db.refresh(version)
    assert version.lifecycle_state == "erase_due" and version.legal_hold


def test_missing_blob_and_post_unlink_db_failure_converge(db, tmp_path, monkeypatch):
    _owner, _collection, _task, _artifact, version, path = _artifact_due(db, tmp_path)
    original_commit = db.commit
    calls = 0

    def fail_final_commit():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated DB outage after unlink")
        original_commit()

    monkeypatch.setattr(db, "commit", fail_final_commit)
    with pytest.raises(RuntimeError, match="simulated DB outage"):
        retention_reaper._erase_artifact_version(
            db, version_id=version.id, now=NOW, artifact_root=str(tmp_path)
        )
    db.rollback()
    assert not path.exists()
    monkeypatch.setattr(db, "commit", original_commit)
    assert retention_reaper._erase_artifact_version(
        db, version_id=version.id, now=NOW, artifact_root=str(tmp_path)
    )
    db.refresh(version)
    assert version.lifecycle_state == "erased"


def test_document_erase_removes_images_chunks_refs_and_reconciles_counters(db, tmp_path):
    _owner, collection, document, blob, image = _document_due(db, tmp_path)
    result = run_retention_batch(
        db, now=NOW, artifact_root=str(tmp_path / "artifacts"),
        ingestion_root=str(tmp_path),
    )
    assert result.documents_erased == 1, result.errors
    assert not blob.exists() and not image.exists()
    db.refresh(document)
    db.refresh(collection)
    assert document.lifecycle_state == "erased"
    assert document.storage_path is None and document.chunk_count == 0
    assert db.execute(text("SELECT count(*) FROM ingestion_images")).scalar() == 0
    assert db.execute(text("SELECT count(*) FROM document_chunks")).scalar() == 0
    assert (collection.document_count, collection.chunk_count) == (0, 0)
    assert collection.bytes_stored == collection.image_count == 0


def test_nonterminal_job_created_after_document_marker_prevents_unlink(db, tmp_path):
    owner, _collection, document, blob, image = _document_due(db, tmp_path)

    def create_active_job(session):
        session.add(IngestionJob(
            arq_job_id="retention-race-job",
            collection_id=document.collection_id,
            document_id=document.id,
            job_type="ingest",
            status="queued",
            enqueued_by=owner.id,
        ))
        session.commit()

    erased = retention_reaper._erase_document(
        db,
        document_id=document.id,
        now=NOW,
        ingestion_root=str(tmp_path),
        after_marker=create_active_job,
    )
    assert not erased
    assert blob.exists() and image.exists()


def test_traversal_is_fail_closed_and_never_unlinks_outside_root(db, tmp_path):
    outside = tmp_path.parent / "retention-outside.txt"
    outside.write_bytes(b"do not delete")
    _owner, _collection, document, blob, _image = _document_due(
        db, tmp_path, image_path="../retention-outside.txt"
    )
    result = run_retention_batch(
        db, now=NOW, artifact_root=str(tmp_path / "artifacts"),
        ingestion_root=str(tmp_path),
    )
    assert result.documents_erased == 0
    assert any("escapes configured root" in error for error in result.errors)
    assert outside.exists() and blob.exists()
    db.refresh(document)
    assert document.lifecycle_state == "erase_due"


def test_safe_path_rejects_symlink_and_expired_lease_blocks_second_runner(db, tmp_path):
    if hasattr((tmp_path / "link"), "symlink_to"):
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            pass
        else:
            with pytest.raises(RetentionSafetyError, match="symlink"):
                safe_ingestion_path(tmp_path, "link/file")

    db.add(RetentionReaperLease(
        lease_name="classified-data-retention",
        lease_token="other",
        lease_expires_at=NOW + timedelta(minutes=5),
        updated_at=NOW,
    ))
    db.commit()
    result = run_retention_batch(
        db, now=NOW, token="this-runner", artifact_root=str(tmp_path),
        ingestion_root=str(tmp_path),
    )
    assert not result.acquired


def test_task_admission_rejects_non_active_collection(db):
    owner = make_user(db, username="retention_task_admission")
    collection = _collection(db, owner, name="retention-task-admission")
    collection.lifecycle_state = "erase_due"
    collection.archived_at = NOW - timedelta(days=2)
    collection.erase_due_at = NOW
    db.commit()

    with pytest.raises(ValueError, match="非 active collection"):
        tasks_module.create_task(
            db,
            requester_user_id=owner.id,
            payload=TaskCreate(
                title="must not race retention",
                task_type="query",
                source_scope="project",
                selected_collection_ids=[collection.id],
            ),
        )
