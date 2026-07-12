"""Initial-upload compensation when Redis enqueue fails.

The queue is an external system, so SQLAlchemy cannot make the enqueue itself
part of the database transaction.  The API compensates by deleting the new
document/job rows.  The content-addressed blob is deliberately retained for a
grace-period GC: another concurrent upload may have observed the same path but
not committed its document reference yet.
"""

from __future__ import annotations

from io import BytesIO
import zipfile

import pytest
from fastapi import HTTPException, UploadFile

from app.api.ingestion import documents
from app.models.ingestion import IngestionCollection, IngestionDocument, IngestionJob
from tests.conftest import make_user


def _collection(
    db,
    user,
    *,
    name: str = "enqueue-rollback",
    classification_level: str = "無機密",
) -> IngestionCollection:
    collection = IngestionCollection(
        name=name,
        chunking_config={"strategy": "fixed", "params": {}},
        embedding_model="test-embedding",
        embedding_dim=4,
        created_by=user.id,
        classification_level=classification_level,
    )
    db.add(collection)
    db.commit()
    db.refresh(collection)
    return collection


async def _enqueue_failure(_document_id: int) -> str:
    raise RuntimeError("redis unavailable")


async def _enqueue_success(document_id: int) -> str:
    return f"job-{document_id}"


def _assert_no_document_or_job(db) -> None:
    db.expire_all()
    assert db.query(IngestionDocument).count() == 0
    assert db.query(IngestionJob).count() == 0


@pytest.mark.asyncio
async def test_single_upload_inherits_locked_collection_classification(
    monkeypatch, tmp_path, db
) -> None:
    user = make_user(db, username="single-classification")
    collection = _collection(
        db, user, classification_level="極機密"
    )
    monkeypatch.setattr(documents, "_UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(documents, "enqueue_ingest_document", _enqueue_success)
    upload = UploadFile(filename="classified.txt", file=BytesIO(b"classified"))

    await documents.upload_document(
        collection.id,
        upload,
        title=None,
        db=db,
        current_user=user,
    )

    row = db.query(IngestionDocument).one()
    assert row.classification_level == "極機密"
    assert row.classification_source == "collection_inherited"
    assert row.classification_latched_at is not None


@pytest.mark.asyncio
async def test_zip_upload_inherits_locked_collection_classification(
    monkeypatch, tmp_path, db
) -> None:
    user = make_user(db, username="zip-classification")
    collection = _collection(db, user, classification_level="機密")
    monkeypatch.setattr(documents, "_UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(documents, "enqueue_ingest_document", _enqueue_success)
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("classified.txt", "classified zip")

    await documents.upload_zip(
        collection.id,
        UploadFile(filename="classified.zip", file=BytesIO(archive.getvalue())),
        preserve_folder_structure=False,
        db=db,
        current_user=user,
    )

    row = db.query(IngestionDocument).one()
    assert row.classification_level == "機密"
    assert row.classification_source == "collection_inherited"
    assert row.classification_latched_at is not None


@pytest.mark.asyncio
async def test_single_upload_enqueue_failure_removes_rows_and_retains_blob_for_gc(
    monkeypatch, tmp_path, db
) -> None:
    user = make_user(db, username="single-enqueue-failure")
    collection = _collection(db, user)
    monkeypatch.setattr(documents, "_UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(documents, "enqueue_ingest_document", _enqueue_failure)
    upload = UploadFile(filename="sample.txt", file=BytesIO(b"hello ingestion"))

    with pytest.raises(HTTPException) as excinfo:
        await documents.upload_document(
            collection.id,
            upload,
            title=None,
            db=db,
            current_user=user,
        )

    assert excinfo.value.status_code == 502
    _assert_no_document_or_job(db)
    blobs = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert len(blobs) == 1
    assert blobs[0].read_bytes() == b"hello ingestion"


@pytest.mark.asyncio
async def test_zip_member_enqueue_failure_removes_rows_and_retains_blob_for_gc(
    monkeypatch, tmp_path, db
) -> None:
    user = make_user(db, username="zip-enqueue-failure")
    collection = _collection(db, user)
    monkeypatch.setattr(documents, "_UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(documents, "enqueue_ingest_document", _enqueue_failure)

    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("sample.txt", "hello from zip")
    upload = UploadFile(filename="sample.zip", file=BytesIO(archive.getvalue()))

    response = await documents.upload_zip(
        collection.id,
        upload,
        preserve_folder_structure=False,
        db=db,
        current_user=user,
    )

    assert response.errors == 1
    assert response.enqueued == 0
    assert response.results[0].status == "error"
    assert response.results[0].document_id is None
    _assert_no_document_or_job(db)
    blobs = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert len(blobs) == 1
    assert blobs[0].read_bytes() == b"hello from zip"


@pytest.mark.asyncio
async def test_enqueue_rollback_keeps_blob_referenced_by_another_collection(
    monkeypatch, tmp_path, db
) -> None:
    user = make_user(db, username="shared-blob-enqueue-failure")
    first = _collection(db, user, name="shared-blob-first")
    second = _collection(db, user, name="shared-blob-second")
    monkeypatch.setattr(documents, "_UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(documents, "enqueue_ingest_document", _enqueue_success)

    content = b"shared content-addressed blob"
    successful_upload = UploadFile(
        filename="first.txt",
        file=BytesIO(content),
    )
    await documents.upload_document(
        first.id,
        successful_upload,
        title=None,
        db=db,
        current_user=user,
    )
    surviving_doc = db.query(IngestionDocument).one()
    surviving_path = surviving_doc.storage_path

    monkeypatch.setattr(documents, "enqueue_ingest_document", _enqueue_failure)
    failed_upload = UploadFile(
        filename="second.txt",
        file=BytesIO(content),
    )
    with pytest.raises(HTTPException) as excinfo:
        await documents.upload_document(
            second.id,
            failed_upload,
            title=None,
            db=db,
            current_user=user,
        )

    assert excinfo.value.status_code == 502
    db.expire_all()
    assert db.query(IngestionDocument).count() == 1
    assert db.query(IngestionJob).count() == 1
    assert surviving_path is not None
    assert documents.os.path.exists(surviving_path)
