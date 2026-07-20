from __future__ import annotations

import hashlib
import os
import stat
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


def test_missing_artifact_root_keeps_version_reference_retryable(db, tmp_path):
    artifact_root = tmp_path / "artifact-root"
    _owner, _collection, _task, _artifact, version, path = _artifact_due(
        db, artifact_root
    )
    held_root = tmp_path / "artifact-root-held"
    artifact_root.rename(held_root)

    result = run_retention_batch(
        db,
        now=NOW,
        artifact_root=str(artifact_root),
        ingestion_root=str(tmp_path / "ingestion-root"),
    )

    assert result.artifacts_erased == 0
    assert any("artifact blob root is unavailable" in error for error in result.errors)
    assert (held_root / path.relative_to(artifact_root)).exists()
    db.refresh(version)
    assert version.lifecycle_state == "erase_due"
    assert version.blob_key is not None


def test_document_erase_removes_images_chunks_refs_and_reconciles_counters(db, tmp_path):
    _owner, collection, document, blob, image = _document_due(db, tmp_path)
    image_dir = image.parent
    stale_temp = image_dir / f"{image.name}.tmp-crashed"
    stale_backup = image_dir / f"{image.name}.bak-crashed"
    unknown_orphan = image_dir / "orphan-unknown.bin"
    other_document_orphan = image_dir.parent / "999" / "orphan.bin"
    other_document_orphan.parent.mkdir(parents=True)
    other_document_orphan.write_bytes(b"other-document-residue")
    stale_temp.write_bytes(b"partial-crash-residue")
    stale_backup.write_bytes(b"old-image")
    unknown_orphan.write_bytes(b"unknown-residue")
    result = run_retention_batch(
        db, now=NOW, artifact_root=str(tmp_path / "artifacts"),
        ingestion_root=str(tmp_path),
    )
    assert result.documents_erased == 1, result.errors
    assert not blob.exists() and not image.exists()
    assert not stale_temp.exists() and not stale_backup.exists()
    assert not unknown_orphan.exists() and not image_dir.exists()
    assert other_document_orphan.exists()
    db.refresh(document)
    db.refresh(collection)
    assert document.lifecycle_state == "erased"
    assert document.storage_path is None and document.chunk_count == 0
    assert db.execute(text("SELECT count(*) FROM ingestion_images")).scalar() == 0
    assert db.execute(text("SELECT count(*) FROM document_chunks")).scalar() == 0
    assert (collection.document_count, collection.chunk_count) == (0, 0)
    assert collection.bytes_stored == collection.image_count == 0


def test_document_image_directory_rmdir_failure_is_not_success(
    db, tmp_path, monkeypatch
):
    _owner, _collection, document, _blob, image = _document_due(db, tmp_path)
    image_dir = image.parent
    original_rmdir = retention_reaper.os.rmdir

    def fail_image_dir_rmdir(path, *, dir_fd=None):
        if path == str(document.id) and dir_fd is not None:
            raise OSError("directory cleanup denied")
        return original_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(retention_reaper.os, "rmdir", fail_image_dir_rmdir)
    result = run_retention_batch(
        db,
        now=NOW,
        artifact_root=str(tmp_path / "artifacts"),
        ingestion_root=str(tmp_path),
    )

    assert result.documents_erased == 0
    assert any("directory cleanup failed" in error for error in result.errors)
    db.refresh(document)
    assert document.lifecycle_state == "erase_due"
    assert image_dir.exists()


def test_document_image_parent_swap_during_unlink_never_touches_outside(
    db, tmp_path, monkeypatch
):
    _owner, _collection, document, blob, image = _document_due(db, tmp_path)
    image_dir = image.parent
    held_dir = image_dir.with_name(f"{image_dir.name}-held")
    outside_dir = tmp_path.parent / "retention-image-race-outside"
    outside_dir.mkdir()
    outside = outside_dir / image.name
    outside.write_bytes(b"must survive parent swap")
    symlink_probe = tmp_path / "retention-image-symlink-probe"
    try:
        symlink_probe.symlink_to(outside_dir, target_is_directory=True)
    except OSError:
        # The actual production target is Linux; keep the suite portable when
        # a test runner cannot create symlinks.
        pytest.skip("symlink creation is unavailable on this platform")
    symlink_probe.unlink()

    original_unlink = retention_reaper.os.unlink
    swapped = False

    def swap_parent_before_first_unlink(path, *, dir_fd=None):
        nonlocal swapped
        if not swapped and dir_fd is not None:
            image_dir.rename(held_dir)
            image_dir.symlink_to(outside_dir, target_is_directory=True)
            swapped = True
        return original_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(
        retention_reaper.os, "unlink", swap_parent_before_first_unlink
    )
    result = run_retention_batch(
        db,
        now=NOW,
        artifact_root=str(tmp_path / "artifacts"),
        ingestion_root=str(tmp_path),
    )

    assert swapped
    assert result.documents_erased == 0
    assert any("replaced" in error for error in result.errors)
    assert outside.read_bytes() == b"must survive parent swap"
    assert blob.exists()
    db.refresh(document)
    assert document.lifecycle_state == "erase_due"


def test_ingestion_root_swap_before_open_fails_closed(db, tmp_path, monkeypatch):
    _owner, _collection, document, blob, image = _document_due(db, tmp_path)
    outside_root = tmp_path.parent / f"{tmp_path.name}-retention-root-race-outside"
    (outside_root / "anila-images" / str(document.id)).mkdir(parents=True)
    outside_image = outside_root / "anila-images" / str(document.id) / image.name
    outside_image.write_bytes(b"must survive root swap")
    try:
        probe = tmp_path.parent / f"{tmp_path.name}-retention-root-symlink-probe"
        probe.symlink_to(outside_root, target_is_directory=True)
        probe.unlink()
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")

    held_root = tmp_path.parent / f"{tmp_path.name}-retention-root-held"
    original_stat = retention_reaper.os.stat
    swapped = False

    def swap_root_before_final_open(path, *, dir_fd=None, follow_symlinks=True):
        nonlocal swapped
        if not swapped and dir_fd is not None and path == tmp_path.name:
            tmp_path.rename(held_root)
            tmp_path.symlink_to(outside_root, target_is_directory=True)
            swapped = True
        return original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(retention_reaper.os, "stat", swap_root_before_final_open)
    result = run_retention_batch(
        db,
        now=NOW,
        artifact_root=str(tmp_path / "artifacts"),
        ingestion_root=str(tmp_path),
    )

    assert swapped
    assert result.documents_erased == 0
    assert any("configured storage root" in error for error in result.errors)
    assert outside_image.read_bytes() == b"must survive root swap"
    assert (held_root / blob.relative_to(tmp_path)).exists()
    assert (held_root / image.relative_to(tmp_path)).exists()
    db.refresh(document)
    assert document.lifecycle_state == "erase_due"


def test_document_storage_parent_swap_never_touches_outside(
    db, tmp_path, monkeypatch
):
    _owner, _collection, document, blob, _image = _document_due(db, tmp_path)
    outside_dir = tmp_path.parent / f"{tmp_path.name}-retention-document-race-outside"
    outside_dir.mkdir()
    outside = outside_dir / blob.name
    outside.write_bytes(b"must survive document parent swap")
    try:
        probe = tmp_path / f"{tmp_path.name}-retention-document-symlink-probe"
        probe.symlink_to(outside_dir, target_is_directory=True)
        probe.unlink()
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")

    held_parent = blob.parent.with_name(f"{blob.parent.name}-held")
    original_unlink = retention_reaper.os.unlink
    swapped = False

    def swap_document_parent_before_unlink(path, *, dir_fd=None):
        nonlocal swapped
        if not swapped and dir_fd is not None and path == blob.name:
            blob.parent.rename(held_parent)
            blob.parent.symlink_to(outside_dir, target_is_directory=True)
            swapped = True
        return original_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(
        retention_reaper.os, "unlink", swap_document_parent_before_unlink
    )
    result = run_retention_batch(
        db,
        now=NOW,
        artifact_root=str(tmp_path / "artifacts"),
        ingestion_root=str(tmp_path),
    )

    assert swapped
    assert result.documents_erased == 0
    assert any("storage directory was replaced" in error for error in result.errors)
    assert outside.read_bytes() == b"must survive document parent swap"
    db.refresh(document)
    assert document.lifecycle_state == "erase_due"


def test_document_fd_cleanup_unsupported_fails_closed(db, tmp_path, monkeypatch):
    _owner, _collection, document, blob, image = _document_due(db, tmp_path)
    monkeypatch.setattr(retention_reaper, "_DIRFD_CLEANUP_SUPPORTED", False)
    result = run_retention_batch(
        db,
        now=NOW,
        artifact_root=str(tmp_path / "artifacts"),
        ingestion_root=str(tmp_path),
    )
    assert result.documents_erased == 0
    assert any("dirfd" in error for error in result.errors)
    assert blob.exists() and image.exists()
    db.refresh(document)
    assert document.lifecycle_state == "erase_due"


def test_missing_ingestion_root_keeps_document_reference_retryable(db, tmp_path):
    ingestion_root = tmp_path / "ingestion-root"
    _owner, _collection, document, blob, image = _document_due(db, ingestion_root)
    held_root = tmp_path / "ingestion-root-held"
    ingestion_root.rename(held_root)

    result = run_retention_batch(
        db,
        now=NOW,
        artifact_root=str(tmp_path / "artifact-root"),
        ingestion_root=str(ingestion_root),
    )

    assert result.documents_erased == 0
    assert any("configured storage root is unavailable" in error for error in result.errors)
    assert (held_root / blob.relative_to(ingestion_root)).exists()
    assert (held_root / image.relative_to(ingestion_root)).exists()
    db.refresh(document)
    assert document.lifecycle_state == "erase_due"
    assert document.storage_path is not None


@pytest.mark.skipif(
    not hasattr(os, "O_PATH") or not hasattr(os, "mkfifo"),
    reason="O_PATH FIFO probe is unavailable on this platform",
)
def test_document_fifo_swap_before_probe_fails_closed(db, tmp_path, monkeypatch):
    _owner, _collection, document, blob, _image = _document_due(db, tmp_path)
    original_open = retention_reaper.os.open
    swapped = False

    def swap_target_before_probe(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if (
            not swapped
            and path == blob.name
            and dir_fd is not None
            and flags & os.O_PATH
        ):
            blob.unlink()
            os.mkfifo(blob)
            swapped = True
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(retention_reaper.os, "open", swap_target_before_probe)
    result = run_retention_batch(
        db,
        now=NOW,
        artifact_root=str(tmp_path / "artifact-root"),
        ingestion_root=str(tmp_path),
    )

    assert swapped
    assert result.documents_erased == 0
    assert any("regular file" in error for error in result.errors)
    assert stat.S_ISFIFO(blob.stat().st_mode)
    db.refresh(document)
    assert document.lifecycle_state == "erase_due"
    assert document.storage_path is not None


def test_document_image_symlink_residue_fails_closed(db, tmp_path):
    _owner, _collection, document, blob, image = _document_due(db, tmp_path)
    image_dir = image.parent
    outside = tmp_path.parent / "retention-image-outside.bin"
    outside.write_bytes(b"must survive")
    link = image_dir / "orphan-link"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")

    result = run_retention_batch(
        db,
        now=NOW,
        artifact_root=str(tmp_path / "artifacts"),
        ingestion_root=str(tmp_path),
    )

    assert result.documents_erased == 0
    assert any("symlink" in error for error in result.errors)
    assert blob.exists() and image.exists() and link.is_symlink()
    assert outside.exists()
    db.refresh(document)
    assert document.lifecycle_state == "erase_due"


def test_document_image_nested_directory_fails_closed(db, tmp_path):
    _owner, _collection, document, blob, image = _document_due(db, tmp_path)
    nested = image.parent / "nested"
    nested.mkdir()
    (nested / "orphan.bin").write_bytes(b"nested-residue")

    result = run_retention_batch(
        db,
        now=NOW,
        artifact_root=str(tmp_path / "artifacts"),
        ingestion_root=str(tmp_path),
    )

    assert result.documents_erased == 0
    assert any("nested directory" in error for error in result.errors)
    assert blob.exists() and image.exists() and nested.exists()
    db.refresh(document)
    assert document.lifecycle_state == "erase_due"


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
