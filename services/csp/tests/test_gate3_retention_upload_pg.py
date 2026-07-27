"""True-PostgreSQL race proofs for Gate 3 retention and artifact upload.

The database URL must use the non-superuser ``csp_app`` runtime role and
point at a disposable database migrated through ``r1_0023`` or later.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import threading
import time
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from starlette.datastructures import Headers, UploadFile

from app.api import artifacts as artifact_api
from app.config import settings
from app.models.artifact import Artifact, ArtifactJob, ArtifactVersion
from app.models.ingestion import (
    IngestionCollection,
    IngestionDocument,
    IngestionDocumentGeneration,
)
from app.models.registered_service import RegisteredService
from app.models.service_client import ServiceClient
from app.models.source_snapshot import SourceSnapshot
from app.models.task import Task, TaskRun
from app.models.user import User
from app.modules.tasks.service import create_task
from app.schemas.contracts.tasks import TaskCreate
from app.services.agent_credential_service import CallerIdentity
from app.services import retention_reaper


_DSN = os.environ.get("ANILA_GATE3_RETENTION_PG_URL")
pytestmark = pytest.mark.skipif(
    not _DSN, reason="ANILA_GATE3_RETENTION_PG_URL is not set"
)


def _engine():
    engine = create_engine(
        _DSN,
        pool_pre_ping=True,
        connect_args={"options": "-c lock_timeout=8000ms"},
    )
    with engine.connect() as connection:
        current_user, is_superuser = connection.execute(
            text(
                "SELECT current_user, rolsuper FROM pg_roles "
                "WHERE rolname = current_user"
            )
        ).one()
    assert current_user == "csp_app"
    assert is_superuser is False
    return engine


def _wait_until_lock_blocked(engine, pid: int) -> None:
    deadline = time.monotonic() + 5
    with engine.connect() as connection:
        while time.monotonic() < deadline:
            wait_event_type = connection.execute(
                text(
                    "SELECT wait_event_type FROM pg_stat_activity "
                    "WHERE pid=:pid"
                ),
                {"pid": pid},
            ).scalar_one_or_none()
            if wait_event_type == "Lock":
                return
            time.sleep(0.02)
    raise AssertionError(f"PostgreSQL backend {pid} never entered a lock wait")


def _user(suffix: str, purpose: str) -> User:
    return User(
        username=f"gate3-a5-{purpose}-{suffix}",
        role="user",
        hashed_password="synthetic-not-a-secret",
        is_active=True,
        is_approved=True,
    )


def test_document_erasure_scopes_force_rls_rows_and_unlinks_bytes(tmp_path: Path):
    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc)
    db = factory()
    document_path = tmp_path / "documents" / f"{suffix}.pdf"
    document_path.parent.mkdir(parents=True, exist_ok=True)
    document_path.write_bytes(b"classified document")

    try:
        owner = _user(suffix, "retention-rls")
        db.add(owner)
        db.flush()
        collection = IngestionCollection(
            name=f"gate3-retention-rls-{suffix}",
            chunking_config={},
            embedding_model="test",
            embedding_fingerprint="sha256:" + ("1" * 64),
            embedding_dim=3,
            created_by=owner.id,
            classification_level="機密",
            document_count=1,
            chunk_count=1,
            bytes_stored=document_path.stat().st_size,
            image_count=1,
        )
        db.add(collection)
        db.flush()
        document = IngestionDocument(
            collection_id=collection.id,
            filename=document_path.name,
            sha256=hashlib.sha256(document_path.read_bytes()).hexdigest(),
            mime_type="application/pdf",
            bytes=document_path.stat().st_size,
            storage_path=str(document_path),
            status="indexed",
            chunk_count=1,
            availability_status="unavailable",
            processing_stage="complete",
            uploaded_by=owner.id,
            classification_level="機密",
            lifecycle_state="archived",
            archived_at=now - timedelta(days=1),
            erase_due_at=now,
        )
        db.add(document)
        db.flush()
        image_relative = f"anila-images/{document.id}/{suffix}.png"
        image_path = tmp_path / image_relative
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(b"classified image")
        generation = IngestionDocumentGeneration(
            document_id=document.id,
            collection_id=collection.id,
            generation_number=1,
            status="active",
            embedding_model="test",
            embedding_fingerprint=collection.embedding_fingerprint,
            embedding_dim=3,
            chunk_count=1,
            activated_at=now,
        )
        db.add(generation)
        db.flush()
        document.active_generation_id = generation.id
        db.execute(
            text("SELECT set_config('anila.collection_id', :cid, true)"),
            {"cid": str(collection.id)},
        )
        db.execute(
            text(
                "INSERT INTO ingestion_images "
                "(collection_id,document_id,image_id,storage_path,mime) "
                "VALUES (:cid,:did,:iid,:path,'image/png')"
            ),
            {
                "cid": collection.id,
                "did": document.id,
                "iid": f"gate3-retention-{suffix}",
                "path": image_relative,
            },
        )
        db.execute(
            text(
                "INSERT INTO document_chunks "
                "(collection_id,document_id,chunk_key,content,generation_id,"
                "is_active_generation) VALUES "
                "(:cid,:did,:key,'classified chunk',:gid,true)"
            ),
            {
                "cid": collection.id,
                "did": document.id,
                "key": f"gate3-retention-{suffix}",
                "gid": generation.id,
            },
        )
        db.execute(
            text(
                "INSERT INTO document_relations "
                "(collection_id,src_document_id,target_ref,relation_type,source) "
                "VALUES (:cid,:did,:target,'cites','rule')"
            ),
            {
                "cid": collection.id,
                "did": document.id,
                "target": f"gate3-retention-target-{suffix}",
            },
        )
        db.commit()
        document_id = int(document.id)
        collection_id = int(collection.id)
        image_dir = tmp_path / "anila-images" / str(document_id)

        assert retention_reaper._erase_document(
            db,
            document_id=document_id,
            now=now,
            ingestion_root=str(tmp_path),
        )
        assert not document_path.exists()
        assert not image_path.exists()
        assert not image_dir.exists()

        db.execute(
            text("SELECT set_config('anila.collection_id', :cid, true)"),
            {"cid": str(collection_id)},
        )
        for table in ("ingestion_images", "document_chunks", "document_relations"):
            assert db.execute(
                text(f"SELECT count(*) FROM {table} WHERE collection_id=:cid"),
                {"cid": collection_id},
            ).scalar_one() == 0
        erased = db.get(IngestionDocument, document_id)
        assert erased is not None and erased.lifecycle_state == "erased"
        refreshed_collection = db.get(IngestionCollection, collection_id)
        assert refreshed_collection is not None
        assert (
            refreshed_collection.document_count,
            refreshed_collection.chunk_count,
            refreshed_collection.bytes_stored,
            refreshed_collection.image_count,
        ) == (0, 0, 0, 0)
    finally:
        db.rollback()
        db.close()
        engine.dispose()


def test_task_admission_waits_for_retention_and_rejects_erasing_collection():
    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    seed = factory()
    owner = _user(suffix, "admission")
    seed.add(owner)
    seed.flush()
    collection = IngestionCollection(
        name=f"gate3-a5-admission-{suffix}",
        chunking_config={},
        embedding_model="test",
        embedding_fingerprint="sha256:" + ("0" * 64),
        embedding_dim=3,
        created_by=owner.id,
        classification_level="機密",
        origin="anilalm",
    )
    seed.add(collection)
    seed.commit()
    owner_id = int(owner.id)
    collection_id = int(collection.id)
    seed.close()

    retention = factory()
    locked = (
        retention.query(IngestionCollection)
        .filter(IngestionCollection.id == collection_id)
        .with_for_update()
        .one()
    )
    locked.lifecycle_state = "erase_due"
    locked.archived_at = datetime.now(timezone.utc)
    locked.erase_due_at = datetime.now(timezone.utc)
    retention.flush()

    admission_pid: list[int] = []

    def admit_task() -> str:
        db = factory()
        try:
            admission_pid.append(db.execute(text("SELECT pg_backend_pid()" )).scalar_one())
            with pytest.raises(ValueError, match="非 active collection") as exc_info:
                create_task(
                    db,
                    requester_user_id=owner_id,
                    payload=TaskCreate(
                        title=f"gate3-a5-must-wait-{suffix}",
                        task_type="query",
                        source_scope="project",
                        selected_collection_ids=[collection_id],
                    ),
                )
            db.rollback()
            return str(exc_info.value)
        finally:
            db.close()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(admit_task)
            deadline = time.monotonic() + 5
            while not admission_pid and time.monotonic() < deadline:
                time.sleep(0.01)
            assert admission_pid
            _wait_until_lock_blocked(engine, admission_pid[0])
            retention.commit()
            assert "非 active collection" in future.result(timeout=10)
    finally:
        retention.rollback()
        retention.close()

    verify = factory()
    try:
        assert (
            verify.query(Task)
            .filter(Task.title == f"gate3-a5-must-wait-{suffix}")
            .count()
            == 0
        )
    finally:
        verify.close()
        engine.dispose()


def test_concurrent_identical_uploads_return_one_authority_and_one_blob(
    monkeypatch, tmp_path: Path
):
    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    seed = factory()
    owner = _user(suffix, "upload")
    seed.add(owner)
    seed.flush()
    service_client = ServiceClient(
        client_name=f"gate3-a5-client-{suffix}",
        client_type="worker",
        service_token_envelope="enc::stub",
        service_token_lookup_hash=hashlib.sha256(suffix.encode()).hexdigest(),
        is_active=True,
        is_legacy=False,
    )
    seed.add(service_client)
    seed.flush()
    service = RegisteredService(
        name=f"Gate 3 A5 Studio {suffix}",
        slug=f"gate3-a5-studio-{suffix}",
        service_type="artifact_tool",
        entry_url="https://studio.invalid",
        data_egress=["artifact"],
        service_client_id=service_client.id,
        is_active=True,
    )
    seed.add(service)
    seed.flush()
    task = Task(
        title=f"gate3-a5-upload-{suffix}",
        task_type="generate_artifact",
        requester_user_id=owner.id,
        status="running",
        source_scope="none",
        selected_service_id=service.slug,
        classification_level="無機密",
    )
    seed.add(task)
    seed.flush()
    snapshot = SourceSnapshot(
        task_id=task.id,
        origin="none",
        source_scope="none",
        classification_level="無機密",
    )
    seed.add(snapshot)
    seed.flush()
    task.source_snapshot_id = snapshot.id
    seed.add(
        TaskRun(
            task_id=task.id,
            run_sequence=1,
            dispatch_target="studio",
            status="running",
            classification_level="無機密",
        )
    )
    durable_attempt = 1
    durable_lease_token = "gate3-a5-race-lease-" + suffix
    job = ArtifactJob(
        job_id=f"gate3-a5-upload-{suffix}",
        owner_user_id=owner.id,
        task_id=task.id,
        source_snapshot_id=snapshot.id,
        artifact_type="report",
        status="running",
        durable_attempt=durable_attempt,
        durable_lease_digest=hashlib.sha256(
            durable_lease_token.encode()
        ).hexdigest(),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    seed.add(job)
    seed.commit()

    caller = artifact_api._ServiceCaller(
        identity=CallerIdentity(
            kind="service_client",
            agent_id=None,
            service_client_id=service_client.id,
            credential_id=service_client.id,
            is_legacy=False,
            used_previous_token=False,
        ),
        service=service,
    )
    content = b"%PDF-1.7\nGate 3 upload race\n%%EOF"
    metadata_json = json.dumps(
        {
            "artifact_type": "report",
            "title": "Gate 3 A5 report",
            "job_id": job.job_id,
            "task_id": task.id,
            "source_snapshot_id": snapshot.id,
            "content_sha256": hashlib.sha256(content).hexdigest(),
            "content_size": len(content),
            "media_type": "application/pdf",
            "original_filename": "report.pdf",
        },
        separators=(",", ":"),
    )
    job_id = job.job_id
    seed.close()

    monkeypatch.setattr(settings, "ARTIFACT_BLOB_STORAGE_PATH", str(tmp_path))
    original_store = artifact_api.store_stream
    first_store_entered = threading.Event()
    release_first_store = threading.Event()
    start = threading.Barrier(3)
    mutex = threading.Lock()
    store_calls = 0
    backend_pids: dict[str, int] = {}

    def delayed_store(*args, **kwargs):
        nonlocal store_calls
        with mutex:
            store_calls += 1
            call_number = store_calls
        if call_number == 1:
            first_store_entered.set()
            assert release_first_store.wait(8)
        return original_store(*args, **kwargs)

    monkeypatch.setattr(artifact_api, "store_stream", delayed_store)

    def upload(label: str):
        db = factory()
        try:
            backend_pids[label] = db.execute(
                text("SELECT pg_backend_pid()")
            ).scalar_one()
            start.wait(timeout=5)
            upload_file = UploadFile(
                file=BytesIO(content),
                filename="report.pdf",
                headers=Headers({"content-type": "application/pdf"}),
            )
            return artifact_api.upload_artifact(
                metadata_json=metadata_json,
                file=upload_file,
                studio_attempt=str(durable_attempt),
                studio_lease_token=durable_lease_token,
                caller=caller,
                db=db,
            )
        finally:
            db.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {
                label: executor.submit(upload, label) for label in ("one", "two")
            }
            start.wait(timeout=5)
            assert first_store_entered.wait(5)
            blocked = False
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with engine.connect() as connection:
                    waits = connection.execute(
                        text(
                            "SELECT wait_event_type FROM pg_stat_activity "
                            "WHERE pid IN (:one, :two)"
                        ),
                        backend_pids,
                    ).scalars().all()
                if "Lock" in waits:
                    blocked = True
                    break
                time.sleep(0.02)
            assert blocked, "duplicate upload never waited on the ArtifactJob row lock"
            release_first_store.set()
            results = [
                futures[label].result(timeout=10) for label in ("one", "two")
            ]
    finally:
        release_first_store.set()

    assert results[0] == results[1]
    assert store_calls == 1
    verify = factory()
    try:
        artifacts = verify.query(Artifact).filter(Artifact.job_id == job_id).all()
        assert len(artifacts) == 1
        assert (
            verify.query(ArtifactVersion)
            .filter(ArtifactVersion.artifact_id == artifacts[0].id)
            .count()
            == 1
        )
    finally:
        verify.close()
        engine.dispose()
    assert sum(1 for path in tmp_path.rglob("*") if path.is_file()) == 1
