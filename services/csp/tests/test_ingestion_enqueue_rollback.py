"""Mutation-sensitive coverage for the durable ingestion outbox."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from io import BytesIO
import zipfile

import pytest
from arq.jobs import JobStatus
from anila_security import verify_queue_proof
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers
from sqlalchemy.orm import sessionmaker

from app.api.ingestion import documents
from app.models.ingestion import (
    IngestionCollection,
    IngestionDocument,
    IngestionJob,
    IngestionOutbox,
)
from app.services import ingestion_outbox
from tests.conftest import make_user


def _collection(
    db,
    user,
    *,
    name: str = "durable-outbox",
    classification_level: str = "無機密",
) -> IngestionCollection:
    collection = IngestionCollection(
        name=name,
        chunking_config={"strategy": "fixed", "params": {}},
        embedding_model="test-embedding",
        embedding_fingerprint="sha256:" + "0" * 64,
        embedding_dim=4,
        created_by=user.id,
        classification_level=classification_level,
    )
    db.add(collection)
    db.commit()
    db.refresh(collection)
    return collection


async def _upload(db, user, collection, tmp_path, *, content=b"hello ingestion"):
    documents._UPLOAD_DIR = str(tmp_path)
    return await documents.upload_document(
        collection.id,
        UploadFile(filename="sample.txt", file=BytesIO(content)),
        title=None,
        db=db,
        current_user=user,
    )


def _assert_durable_triplet(db) -> tuple[IngestionDocument, IngestionJob, IngestionOutbox]:
    db.expire_all()
    document = db.query(IngestionDocument).one()
    job = db.query(IngestionJob).one()
    intent = db.query(IngestionOutbox).one()
    assert job.document_id == document.id
    assert job.status == "dispatch_pending"
    assert job.arq_job_id == f"ingest-job-{job.id}-attempt-1"
    assert intent.ingestion_job_id == job.id
    assert intent.arq_job_id == job.arq_job_id
    assert intent.attempt_number == 1
    assert intent.payload == {
        "document_id": document.id,
        "ingestion_job_id": job.id,
        "attempt_number": 1,
    }
    assert intent.status == "pending"
    return document, job, intent


@pytest.mark.asyncio
async def test_relay_task_is_cancellable_for_lifespan_shutdown(monkeypatch) -> None:
    async def no_work() -> bool:
        return False

    monkeypatch.setattr(ingestion_outbox, "relay_once", no_work)
    monkeypatch.setattr(ingestion_outbox, "_POLL_SECONDS", 3600)
    task = await ingestion_outbox.start_ingestion_outbox_relay()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.done()


@pytest.mark.asyncio
async def test_relay_loop_survives_transient_claim_failure(monkeypatch) -> None:
    calls = 0
    recovered = asyncio.Event()

    async def flaky_relay_once() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("database temporarily unavailable")
        recovered.set()
        return False

    monkeypatch.setattr(ingestion_outbox, "relay_once", flaky_relay_once)
    monkeypatch.setattr(ingestion_outbox, "_POLL_SECONDS", 0)
    task = await ingestion_outbox.start_ingestion_outbox_relay()
    await asyncio.wait_for(recovered.wait(), timeout=1)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_single_upload_inherits_locked_collection_classification(
    monkeypatch, tmp_path, db
) -> None:
    user = make_user(db, username="redis-down-upload")
    collection = _collection(db, user, classification_level="極機密")

    async def redis_down():
        raise RuntimeError("redis unavailable")

    # A mutation that reintroduces request-time Redis access makes this fail.
    monkeypatch.setattr(ingestion_outbox, "_get_pool", redis_down)
    response = await _upload(db, user, collection, tmp_path)

    document, _, _ = _assert_durable_triplet(db)
    assert response.id == document.id
    assert document.classification_level == "極機密"
    assert document.classification_source == "collection_inherited"


@pytest.mark.asyncio
async def test_upload_rejects_extension_and_mime_spoof_before_persistence(
    tmp_path, db
) -> None:
    user = make_user(db, username="sniff-single-upload")
    collection = _collection(db, user)
    documents._UPLOAD_DIR = str(tmp_path)

    with pytest.raises(HTTPException, match="signature") as extension_error:
        await documents.upload_document(
            collection.id,
            UploadFile(filename="fake.txt", file=BytesIO(b"%PDF-1.7\n%%EOF")),
            title=None,
            db=db,
            current_user=user,
        )
    assert extension_error.value.status_code == 422

    with pytest.raises(HTTPException, match="MIME") as mime_error:
        await documents.upload_document(
            collection.id,
            UploadFile(
                filename="note.txt",
                file=BytesIO(b"safe text"),
                headers=Headers({"content-type": "image/png"}),
            ),
            title=None,
            db=db,
            current_user=user,
        )
    assert mime_error.value.status_code == 422
    assert db.query(IngestionDocument).count() == 0
    assert db.query(IngestionJob).count() == 0
    assert db.query(IngestionOutbox).count() == 0
    assert not list(tmp_path.rglob("*"))


@pytest.mark.asyncio
async def test_zip_upload_rejects_traversal_before_member_persistence(tmp_path, db) -> None:
    user = make_user(db, username="sniff-zip-traversal")
    collection = _collection(db, user)
    documents._UPLOAD_DIR = str(tmp_path)
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escape.txt", "escape")

    with pytest.raises(HTTPException, match="unsafe ZIP") as error:
        await documents.upload_zip(
            collection.id,
            UploadFile(filename="batch.zip", file=BytesIO(archive.getvalue())),
            preserve_folder_structure=False,
            db=db,
            current_user=user,
        )
    assert error.value.status_code == 422
    assert db.query(IngestionDocument).count() == 0
    assert db.query(IngestionJob).count() == 0
    assert db.query(IngestionOutbox).count() == 0
    assert not list(tmp_path.rglob("*"))


@pytest.mark.asyncio
async def test_zip_upload_rejects_wrong_member_container_without_persisting(
    tmp_path, db
) -> None:
    user = make_user(db, username="sniff-zip-member")
    collection = _collection(db, user)
    documents._UPLOAD_DIR = str(tmp_path)
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("fake.docx", b"%PDF-1.7\n%%EOF")

    response = await documents.upload_zip(
        collection.id,
        UploadFile(filename="batch.zip", file=BytesIO(archive.getvalue())),
        preserve_folder_structure=False,
        db=db,
        current_user=user,
    )
    assert response.enqueued == 0
    assert response.errors == 1
    assert "content rejected" in response.results[0].detail
    assert db.query(IngestionDocument).count() == 0
    assert db.query(IngestionJob).count() == 0
    assert db.query(IngestionOutbox).count() == 0
    assert not list(tmp_path.rglob("*"))


def test_document_job_and_outbox_rollback_together(db) -> None:
    user = make_user(db, username="transaction-rollback")
    collection = _collection(db, user)
    document = IngestionDocument(
        collection_id=collection.id,
        filename="rollback.txt",
        sha256="a" * 64,
        status="pending",
        chunk_count=0,
        uploaded_by=user.id,
        classification_level="無機密",
    )
    db.add(document)
    ingestion_outbox.create_ingestion_dispatch(
        db,
        document=document,
        enqueued_by=user.id,
    )
    db.rollback()

    assert db.query(IngestionDocument).count() == 0
    assert db.query(IngestionJob).count() == 0
    assert db.query(IngestionOutbox).count() == 0


@pytest.mark.asyncio
async def test_relay_observes_job_row_before_enqueue(tmp_path, db) -> None:
    user = make_user(db, username="job-before-redis")
    collection = _collection(db, user)
    await _upload(db, user, collection, tmp_path)
    Session = sessionmaker(bind=db.get_bind())

    class Pool:
        async def enqueue_job(
            self,
            task,
            document_id,
            ingestion_job_id,
            attempt_number,
            queue_proof,
            *,
            _job_id,
        ):
            verify_queue_proof(
                ingestion_outbox.settings.INGESTION_QUEUE_HMAC_KEY,
                task_name=task,
                payload={
                    "document_id": document_id,
                    "ingestion_job_id": ingestion_job_id,
                    "attempt_number": attempt_number,
                },
                proof=queue_proof,
            )
            check = Session()
            try:
                job = check.query(IngestionJob).filter_by(arq_job_id=_job_id).one()
                assert job.document_id == document_id
                assert job.id == ingestion_job_id
                assert attempt_number == 1
                assert job.status == "queued"
                claimed = (
                    check.query(IngestionJob)
                    .filter(
                        IngestionJob.id == ingestion_job_id,
                        IngestionJob.status == "queued",
                        IngestionJob.attempt_count == 0,
                    )
                    .update(
                        {
                            "status": "succeeded",
                            "attempt_count": 1,
                            "completed_at": datetime.now(timezone.utc),
                        },
                        synchronize_session=False,
                    )
                )
                assert claimed == 1
                check.commit()
            finally:
                check.close()
            return object()

    assert await ingestion_outbox.relay_once(session_factory=Session, pool=Pool())
    db.expire_all()
    assert db.query(IngestionOutbox).one().status == "published"
    assert db.query(IngestionJob).one().status == "succeeded"


@pytest.mark.asyncio
async def test_redis_failure_keeps_intent_with_backoff(tmp_path, db) -> None:
    user = make_user(db, username="relay-redis-failure")
    collection = _collection(db, user)
    await _upload(db, user, collection, tmp_path)
    Session = sessionmaker(bind=db.get_bind())
    before = datetime.now(timezone.utc)

    class FailingPool:
        async def enqueue_job(self, *_args, **_kwargs):
            raise ConnectionError("redis unavailable")

    assert await ingestion_outbox.relay_once(
        session_factory=Session,
        pool=FailingPool(),
    )
    db.expire_all()
    intent = db.query(IngestionOutbox).one()
    assert intent.status == "pending"
    assert intent.attempt_count == 1
    assert intent.lease_token is None
    assert intent.last_error == "ConnectionError: redis unavailable"
    available_at = intent.available_at
    if available_at.tzinfo is None:
        available_at = available_at.replace(tzinfo=timezone.utc)
    assert available_at > before
    assert db.query(IngestionJob).one().status == "queued"

    # A Redis outage leaves a replayable queued DB job; the next relay pass
    # publishes the same deterministic attempt without needing a new request.
    intent.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    class RecoveredPool:
        async def enqueue_job(self, *_args, **_kwargs):
            return object()

    assert await ingestion_outbox.relay_once(
        session_factory=Session, pool=RecoveredPool()
    )
    db.expire_all()
    assert db.query(IngestionOutbox).one().status == "published"
    assert db.query(IngestionJob).one().status == "queued"


@pytest.mark.asyncio
async def test_retry_attempt_is_queued_before_redis_and_not_by_publish(tmp_path, db):
    user = make_user(db, username="retry-before-redis")
    collection = _collection(db, user)
    await _upload(db, user, collection, tmp_path)
    job = db.query(IngestionJob).one()
    db.query(IngestionOutbox).delete()
    job.status = "retry_wait"
    job.attempt_count = 1
    job.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    job.arq_job_id = f"ingest-job-{job.id}-attempt-2"
    db.add(
        IngestionOutbox(
            ingestion_job_id=job.id,
            attempt_number=2,
            arq_job_id=job.arq_job_id,
            task_name="ingest_document",
            payload={
                "document_id": job.document_id,
                "ingestion_job_id": job.id,
                "attempt_number": 2,
            },
            status="pending",
        )
    )
    db.commit()
    Session = sessionmaker(bind=db.get_bind())

    class Pool:
        async def enqueue_job(
            self,
            task,
            document_id,
            ingestion_job_id,
            attempt_number,
            queue_proof,
            *,
            _job_id,
        ):
            verify_queue_proof(
                ingestion_outbox.settings.INGESTION_QUEUE_HMAC_KEY,
                task_name=task,
                payload={
                    "document_id": document_id,
                    "ingestion_job_id": ingestion_job_id,
                    "attempt_number": attempt_number,
                },
                proof=queue_proof,
            )
            check = Session()
            try:
                current = check.get(IngestionJob, ingestion_job_id)
                assert current.status == "queued"
                assert current.attempt_count == 1
                assert attempt_number == 2
            finally:
                check.close()
            return object()

    assert await ingestion_outbox.relay_once(session_factory=Session, pool=Pool())
    db.expire_all()
    assert db.query(IngestionJob).one().status == "queued"
    assert db.query(IngestionOutbox).one().status == "published"


@pytest.mark.asyncio
async def test_crash_after_enqueue_replays_duplicate_then_publishes(
    monkeypatch, tmp_path, db
) -> None:
    user = make_user(db, username="relay-crash-replay")
    collection = _collection(db, user)
    await _upload(db, user, collection, tmp_path)
    Session = sessionmaker(bind=db.get_bind())

    first_claim = ingestion_outbox._claim_next(Session)
    assert first_claim is not None

    class FirstPool:
        async def enqueue_job(self, *_args, **_kwargs):
            return object()

    # Simulate process death after Redis accepted the job but before the DB
    # acknowledgement: dispatch only, leaving the lease in place.
    await ingestion_outbox._dispatch_claim(first_claim, FirstPool())
    db.query(IngestionOutbox).update(
        {"lease_expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)},
        synchronize_session=False,
    )
    db.commit()

    replay_claim = ingestion_outbox._claim_next(Session)
    assert replay_claim is not None
    assert replay_claim.arq_job_id == first_claim.arq_job_id

    class DuplicatePool:
        async def enqueue_job(self, *_args, **_kwargs):
            return None

    class ExistingJob:
        def __init__(self, job_id, *, redis):
            assert job_id == replay_claim.arq_job_id
            assert isinstance(redis, DuplicatePool)

        async def status(self):
            return JobStatus.queued

    monkeypatch.setattr(ingestion_outbox, "Job", ExistingJob)
    await ingestion_outbox._dispatch_claim(replay_claim, DuplicatePool())
    ingestion_outbox._mark_published(replay_claim, Session)

    db.expire_all()
    intent = db.query(IngestionOutbox).one()
    assert intent.status == "published"
    assert intent.attempt_count == 2
    assert intent.published_at is not None


@pytest.mark.asyncio
async def test_stale_published_queued_job_replays_only_when_redis_not_found(
    monkeypatch, tmp_path, db
) -> None:
    user = make_user(db, username="relay-stale-missing")
    collection = _collection(db, user)
    await _upload(db, user, collection, tmp_path)
    Session = sessionmaker(bind=db.get_bind())

    class InitialPool:
        async def enqueue_job(self, *_args, **_kwargs):
            return object()

    assert await ingestion_outbox.relay_once(
        session_factory=Session, pool=InitialPool()
    )
    intent = db.query(IngestionOutbox).one()
    original_arq_id = intent.arq_job_id
    intent.published_at = datetime.now(timezone.utc) - timedelta(seconds=121)
    db.commit()
    monkeypatch.setattr(
        ingestion_outbox.settings, "INGESTION_OUTBOX_STALE_SECONDS", 120
    )

    class MissingJob:
        def __init__(self, job_id, *, redis):
            assert job_id == original_arq_id
            assert isinstance(redis, ReplayPool)

        async def status(self):
            return JobStatus.not_found

    class ReplayPool:
        calls = 0

        async def enqueue_job(self, *_args, **kwargs):
            self.calls += 1
            assert kwargs["_job_id"] == original_arq_id
            return object()

    monkeypatch.setattr(ingestion_outbox, "Job", MissingJob)
    replay_pool = ReplayPool()
    assert await ingestion_outbox.relay_once(
        session_factory=Session, pool=replay_pool
    )
    assert replay_pool.calls == 1
    db.expire_all()
    intent = db.query(IngestionOutbox).one()
    assert intent.status == "published"
    assert intent.arq_job_id == original_arq_id
    assert intent.attempt_number == 1
    assert db.query(IngestionOutbox).count() == 1
    assert db.query(IngestionJob).one().status == "queued"


@pytest.mark.asyncio
async def test_stale_published_intent_does_not_enqueue_existing_redis_job(
    monkeypatch, tmp_path, db
) -> None:
    user = make_user(db, username="relay-stale-existing")
    collection = _collection(db, user)
    await _upload(db, user, collection, tmp_path)
    Session = sessionmaker(bind=db.get_bind())

    class InitialPool:
        async def enqueue_job(self, *_args, **_kwargs):
            return object()

    assert await ingestion_outbox.relay_once(
        session_factory=Session, pool=InitialPool()
    )
    intent = db.query(IngestionOutbox).one()
    intent.published_at = datetime.now(timezone.utc) - timedelta(seconds=121)
    db.commit()
    monkeypatch.setattr(
        ingestion_outbox.settings, "INGESTION_OUTBOX_STALE_SECONDS", 120
    )

    class ExistingJob:
        def __init__(self, _job_id, *, redis):
            assert isinstance(redis, ProbePool)

        async def status(self):
            return JobStatus.queued

    class ProbePool:
        async def enqueue_job(self, *_args, **_kwargs):
            raise AssertionError("existing Redis job must not be enqueued again")

    monkeypatch.setattr(ingestion_outbox, "Job", ExistingJob)
    assert await ingestion_outbox.relay_once(
        session_factory=Session, pool=ProbePool()
    )
    db.expire_all()
    assert db.query(IngestionOutbox).one().status == "published"


@pytest.mark.asyncio
async def test_stale_published_terminal_job_is_never_replayed(
    monkeypatch, tmp_path, db
) -> None:
    user = make_user(db, username="relay-stale-terminal")
    collection = _collection(db, user)
    await _upload(db, user, collection, tmp_path)
    Session = sessionmaker(bind=db.get_bind())

    class InitialPool:
        async def enqueue_job(self, *_args, **_kwargs):
            return object()

    assert await ingestion_outbox.relay_once(
        session_factory=Session, pool=InitialPool()
    )
    intent = db.query(IngestionOutbox).one()
    intent.published_at = datetime.now(timezone.utc) - timedelta(seconds=121)
    job = db.query(IngestionJob).one()
    job.status = "succeeded"
    job.attempt_count = 1
    job.completed_at = datetime.now(timezone.utc)
    db.commit()
    monkeypatch.setattr(
        ingestion_outbox.settings, "INGESTION_OUTBOX_STALE_SECONDS", 120
    )

    class NeverPool:
        async def enqueue_job(self, *_args, **_kwargs):
            raise AssertionError("terminal DB job must never be replayed")

    assert not await ingestion_outbox.relay_once(
        session_factory=Session, pool=NeverPool()
    )
    db.expire_all()
    assert db.query(IngestionOutbox).count() == 0
    assert db.query(IngestionJob).one().status == "succeeded"


@pytest.mark.asyncio
async def test_terminal_history_cannot_starve_new_pending_intent(
    monkeypatch, tmp_path, db
) -> None:
    user = make_user(db, username="relay-terminal-history")
    collection = _collection(db, user)
    stale_at = datetime.now(timezone.utc) - timedelta(seconds=121)

    # More rows than a normal minute of relay polling makes the starvation
    # deterministic: the old implementation selected the lowest stale id,
    # refreshed it, slept, and never reached the new pending intent.
    for index in range(150):
        job = IngestionJob(
            arq_job_id=f"historical-terminal-{index}",
            collection_id=collection.id,
            document_id=None,
            job_type="ingest",
            status="succeeded",
            attempt_count=1,
            completed_at=stale_at,
            enqueued_by=user.id,
        )
        db.add(job)
        db.flush()
        db.add(
            IngestionOutbox(
                ingestion_job_id=job.id,
                attempt_number=1,
                arq_job_id=job.arq_job_id,
                task_name="ingest_document",
                payload={
                    "document_id": 0,
                    "ingestion_job_id": job.id,
                    "attempt_number": 1,
                },
                status="published",
                published_at=stale_at,
            )
        )
    db.commit()

    await _upload(db, user, collection, tmp_path, content=b"new pending work")
    pending = (
        db.query(IngestionOutbox)
        .filter(IngestionOutbox.status == "pending")
        .one()
    )
    pending_id = pending.id
    Session = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr(
        ingestion_outbox.settings, "INGESTION_OUTBOX_STALE_SECONDS", 120
    )

    class Pool:
        calls = 0

        async def enqueue_job(self, *_args, **_kwargs):
            self.calls += 1
            return object()

    pool = Pool()
    assert await ingestion_outbox.relay_once(session_factory=Session, pool=pool)
    db.expire_all()
    assert pool.calls == 1
    assert db.query(IngestionOutbox).count() == 1
    intent = db.get(IngestionOutbox, pending_id)
    assert intent is not None and intent.status == "published"


@pytest.mark.asyncio
async def test_zip_members_each_persist_durable_intent(tmp_path, db) -> None:
    user = make_user(db, username="zip-durable-outbox")
    collection = _collection(db, user, classification_level="機密")
    documents._UPLOAD_DIR = str(tmp_path)
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("first.txt", "first")
        zf.writestr("second.txt", "second")

    response = await documents.upload_zip(
        collection.id,
        UploadFile(filename="classified.zip", file=BytesIO(archive.getvalue())),
        preserve_folder_structure=False,
        db=db,
        current_user=user,
    )

    assert response.enqueued == 2
    assert response.errors == 0
    assert db.query(IngestionDocument).count() == 2
    assert db.query(IngestionJob).count() == 2
    assert db.query(IngestionOutbox).count() == 2
    assert {row.status for row in db.query(IngestionOutbox).all()} == {"pending"}
    assert {
        row.classification_level for row in db.query(IngestionDocument).all()
    } == {"機密"}


@pytest.mark.asyncio
async def test_reprocess_uses_same_durable_dispatch_helper(tmp_path, db) -> None:
    user = make_user(db, username="reprocess-durable-outbox")
    collection = _collection(db, user)
    response = await _upload(db, user, collection, tmp_path)
    document = db.get(IngestionDocument, response.id)
    document.status = "failed"
    document.error_message = "old failure"
    db.commit()

    await documents.reprocess_document(
        document.id,
        db=db,
        current_user=user,
    )

    db.expire_all()
    assert db.get(IngestionDocument, document.id).status == "pending"
    assert db.query(IngestionJob).count() == 2
    assert db.query(IngestionOutbox).count() == 2
    assert {
        row.status for row in db.query(IngestionOutbox).all()
    } == {"pending"}
