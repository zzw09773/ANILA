"""Gate 3 A1 security contract for durable Studio runtime delegation."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from sqlalchemy import text

from app.api import studio_runtime
from app.api.ingestion.search import (
    ImageSearchRequest,
    ImageSearchResponse,
    SearchRequest,
    SearchResponse,
)
from app.models.artifact import ArtifactJob
from app.models.clearance import (
    ClearanceGrant,
    ClearanceGrantCompartment,
    CollectionAccessGrant,
    CollectionRequiredCompartment,
    SecurityCompartment,
)
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.models.registered_service import RegisteredService
from app.models.service_client import ServiceClient
from app.models.source_snapshot import SourceSnapshot
from app.models.task import Task, TaskRun
from app.services import agent_credential_service
from app.services.proxy.task_link import attach_running_task_run
from tests.conftest import make_user


_TOKEN = "csk-test-studio-runtime"


@pytest.fixture
def runtime_scope(db, monkeypatch):
    user = make_user(db, username="studio_runtime_owner")
    client = ServiceClient(
        client_name="anila-studio-runtime",
        client_type="worker",
        description="test",
        service_token_envelope="test-envelope",
        service_token_lookup_hash="a" * 64,
        is_legacy=False,
        is_active=True,
    )
    db.add(client)
    db.flush()
    service = RegisteredService(
        name="Studio Runtime",
        slug="anila-studio-runtime",
        service_type="artifact_tool",
        entry_url="http://anila-studio:8100",
        data_ingress=[],
        data_egress=["studio_runtime"],
        allowed_origins=[],
        required_roles=[],
        service_admin_user_ids=[],
        service_client_id=client.id,
        is_public=False,
        is_active=True,
        config_source="env_seeded",
        db_editable_fields=[],
    )
    db.add(service)
    collection = IngestionCollection(
        name="runtime collection",
        chunking_config={},
        embedding_model="embed-test",
        embedding_fingerprint=f"sha256:{'1' * 64}",
        embedding_dim=3,
        created_by=user.id,
        classification_level="無機密",
    )
    db.add(collection)
    db.flush()
    task = Task(
        title="runtime task",
        task_type="generate_artifact",
        requester_user_id=user.id,
        status="running",
        source_scope="project",
        selected_collection_ids=[collection.id],
        selected_service_id="anila-studio-runtime",
        requested_output_type="report",
        classification_level="無機密",
    )
    db.add(task)
    db.flush()
    snapshot = SourceSnapshot(
        task_id=task.id,
        origin="collection",
        source_scope="project",
        collection_ids=[collection.id],
        document_ids=[101, 102],
        classification_level="無機密",
    )
    db.add(snapshot)
    db.flush()
    task.source_snapshot_id = snapshot.id
    now = datetime.now(timezone.utc)
    job = ArtifactJob(
        job_id="studio-runtime-job",
        owner_user_id=user.id,
        collection_id=collection.id,
        task_id=task.id,
        source_snapshot_id=snapshot.id,
        artifact_type="report",
        status="running",
        trace_id=task.trace_id,
        durable_attempt=1,
        durable_lease_digest=hashlib.sha256(b"lease-token-123456789").hexdigest(),
        expires_at=now + timedelta(hours=1),
    )
    db.add(job)
    run = TaskRun(
        task_id=task.id,
        run_sequence=1,
        dispatch_target="studio",
        status="running",
        started_at=now,
        classification_level=task.classification_level,
    )
    db.add(run)
    grant = ClearanceGrant(
        subject_user_id=user.id,
        max_classification_level="無機密",
        valid_from=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=1),
        basis_ticket="TEST-STUDIO-RUNTIME",
        issued_by_user_id=user.id,
    )
    db.add(grant)
    db.flush()
    db.add(
        CollectionAccessGrant(
            clearance_grant_id=grant.id,
            collection_id=collection.id,
            membership_granted=True,
            need_to_know=True,
            basis_ticket="TEST-STUDIO-RUNTIME",
            issued_by_user_id=user.id,
        )
    )
    db.commit()

    def verify(_db, *, token):
        if token != _TOKEN:
            return None
        return agent_credential_service.CallerIdentity(
            kind="service_client",
            agent_id=None,
            service_client_id=client.id,
            credential_id=client.id,
            is_legacy=False,
            used_previous_token=False,
        )

    monkeypatch.setattr(agent_credential_service, "verify_service_token", verify)
    kwargs = {
        "x_csp_service_token": _TOKEN,
        "job_id": job.job_id,
        "artifact_type": job.artifact_type,
        "requester_user_id": str(user.id),
        "studio_collection_id": str(collection.id),
        "studio_task_id": str(task.id),
        "canonical_task_id": str(task.id),
        "studio_snapshot_id": str(snapshot.id),
        "trace_id": task.trace_id,
        "studio_attempt": "1",
        "studio_lease_token": "lease-token-123456789",
        "db": db,
    }
    return SimpleNamespace(
        user=user,
        client=client,
        service=service,
        collection=collection,
        task=task,
        snapshot=snapshot,
        job=job,
        run=run,
        grant=grant,
        kwargs=kwargs,
    )


def _bind(scope, **changes):
    kwargs = dict(scope.kwargs)
    kwargs.update(changes)
    return studio_runtime.require_runtime_binding(**kwargs)


def test_exact_named_runtime_capability_is_required(runtime_scope, db):
    binding = _bind(runtime_scope)
    assert binding.job.job_id == runtime_scope.job.job_id
    assert binding.user.id == runtime_scope.user.id

    runtime_scope.service.data_egress = ["studio_runtime", "artifact"]
    db.commit()
    with pytest.raises(HTTPException) as exc:
        _bind(runtime_scope)
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc:
        _bind(runtime_scope, x_csp_service_token="csk-wrong")
    assert exc.value.status_code == 401


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("job_id", "other-job"),
        ("artifact_type", "slides"),
        ("requester_user_id", "999999"),
        ("studio_collection_id", "999999"),
        ("studio_task_id", "999999"),
        ("canonical_task_id", "999999"),
        ("studio_snapshot_id", "999999"),
        ("trace_id", "other-trace"),
        ("trace_id", None),
        ("studio_attempt", "2"),
        ("studio_lease_token", "different-lease-token-123"),
    ],
)
def test_any_scope_header_tamper_fails_closed(runtime_scope, field, value):
    with pytest.raises(HTTPException) as exc:
        _bind(runtime_scope, **{field: value})
    assert exc.value.status_code in {400, 403, 404}


@pytest.mark.parametrize(
    ("target", "terminal"),
    [
        ("task", "completed"),
        ("task", "failed"),
        ("task", "cancelled"),
        ("task", "blocked_by_policy"),
        ("job", "completed"),
        ("job", "failed"),
        ("job", "cancelled"),
    ],
)
def test_terminal_task_or_job_cannot_use_runtime(runtime_scope, db, target, terminal):
    setattr(getattr(runtime_scope, target), "status", terminal)
    db.commit()
    with pytest.raises(HTTPException) as exc:
        _bind(runtime_scope)
    assert exc.value.status_code == 403


def test_classification_and_compartment_are_rechecked_each_call(runtime_scope, db):
    runtime_scope.collection.classification_level = "機密"
    db.commit()
    with pytest.raises(HTTPException) as exc:
        _bind(runtime_scope)
    assert exc.value.status_code == 403

    runtime_scope.grant.max_classification_level = "機密"
    compartment = SecurityCompartment(
        code="PROJECT_X",
        name="Project X",
        created_by_user_id=runtime_scope.user.id,
    )
    db.add(compartment)
    db.flush()
    db.add(
        CollectionRequiredCompartment(
            collection_id=runtime_scope.collection.id,
            compartment_id=compartment.id,
            basis_ticket="TEST-STUDIO-RUNTIME",
            assigned_by_user_id=runtime_scope.user.id,
        )
    )
    db.commit()
    with pytest.raises(HTTPException) as exc:
        _bind(runtime_scope)
    assert exc.value.status_code == 403

    db.add(
        ClearanceGrantCompartment(
            clearance_grant_id=runtime_scope.grant.id,
            compartment_id=compartment.id,
        )
    )
    db.commit()
    assert _bind(runtime_scope).collection.id == runtime_scope.collection.id


def test_sink_admission_rechecks_cancel_and_expiry(runtime_scope, db):
    binding = _bind(runtime_scope)
    runtime_scope.job.status = "cancelled"
    db.commit()
    with pytest.raises(HTTPException) as exc:
        studio_runtime._admit_runtime_sink(db, binding)
    assert exc.value.status_code == 409

    runtime_scope.job.status = "running"
    runtime_scope.job.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    binding = SimpleNamespace(**{**binding.__dict__, "job": runtime_scope.job})
    with pytest.raises(HTTPException) as exc:
        studio_runtime._admit_runtime_sink(db, binding)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_text_and_image_search_are_forced_to_snapshot_subset(
    runtime_scope, db, monkeypatch
):
    binding = _bind(runtime_scope)
    seen: list[tuple[str, list[int] | None]] = []

    async def fake_text(*, payload, **_kwargs):
        seen.append(("text", payload.document_ids))
        return SearchResponse(
            query=payload.query,
            embedding_model="embed-test",
            embedding_fingerprint=f"sha256:{'1' * 64}",
            embedding_dim=3,
            results=[],
        )

    async def fake_images(*, payload, **_kwargs):
        seen.append(("image", payload.document_ids))
        return ImageSearchResponse(
            query=payload.query,
            embedding_model="embed-test",
            embedding_fingerprint=f"sha256:{'1' * 64}",
            embedding_dim=3,
            results=[],
        )

    monkeypatch.setattr(studio_runtime, "search_collection", fake_text)
    monkeypatch.setattr(studio_runtime, "search_collection_images", fake_images)
    await studio_runtime.runtime_search(
        runtime_scope.collection.id,
        SearchRequest(query="q"),
        binding,
        db,
    )
    await studio_runtime.runtime_image_search(
        runtime_scope.collection.id,
        ImageSearchRequest(query="q"),
        binding,
        db,
    )
    await studio_runtime.runtime_search(
        runtime_scope.collection.id,
        SearchRequest(query="q", document_ids=[101]),
        binding,
        db,
    )
    assert seen == [
        ("text", [101, 102]),
        ("image", [101, 102]),
        ("text", [101]),
    ]

    with pytest.raises(HTTPException) as exc:
        await studio_runtime.runtime_image_search(
            runtime_scope.collection.id,
            ImageSearchRequest(query="q", document_ids=[999]),
            binding,
            db,
        )
        assert exc.value.status_code == 403


def test_image_blob_uses_canonical_document_live_auth_and_honors_revoke(
    runtime_scope, db, monkeypatch
):
    binding = _bind(runtime_scope)
    document = IngestionDocument(
        id=101,
        collection_id=runtime_scope.collection.id,
        filename="shared.pdf",
        sha256="a" * 64,
        status="indexed",
        lifecycle_state="active",
    )
    db.add(document)
    db.execute(
        text(
            "CREATE TABLE IF NOT EXISTS ingestion_images ("
            "id INTEGER PRIMARY KEY, collection_id INTEGER NOT NULL, "
            "document_id INTEGER NOT NULL, storage_path TEXT NOT NULL, mime TEXT)"
        )
    )
    db.execute(
        text(
            "INSERT INTO ingestion_images "
            "(id, collection_id, document_id, storage_path, mime) "
            "VALUES (501, :collection_id, 101, 'images/shared.png', 'image/png')"
        ),
        {"collection_id": runtime_scope.collection.id},
    )
    db.commit()
    auth_calls: list[tuple[list[int], bool]] = []
    streamed: list[str] = []

    def allow(_db, *, principal, collection_id, requested_ids, reject_denied):
        assert principal.user.id == runtime_scope.user.id
        assert collection_id == runtime_scope.collection.id
        auth_calls.append((requested_ids, reject_denied))
        return requested_ids

    def stream(**kwargs):
        streamed.append(kwargs["storage_path"])
        return "blob"

    monkeypatch.setattr(studio_runtime, "_authorized_document_ids", allow)
    monkeypatch.setattr(studio_runtime, "stream_scoped_image_blob", stream)
    assert studio_runtime.runtime_image_blob(501, binding, db) == "blob"
    assert auth_calls == [([101], True)]
    assert streamed == ["images/shared.png"]

    def deny(*_args, **_kwargs):
        raise HTTPException(status_code=403, detail="document grant revoked")

    monkeypatch.setattr(studio_runtime, "_authorized_document_ids", deny)
    with pytest.raises(HTTPException) as exc:
        studio_runtime.runtime_image_blob(501, binding, db)
    assert exc.value.status_code == 403
    assert streamed == ["images/shared.png"]


@pytest.mark.asyncio
async def test_chat_delegation_attaches_existing_run_without_terminalizing(
    runtime_scope, db, monkeypatch
):
    binding = _bind(runtime_scope)
    header_values = {
        "x-csp-service-token": _TOKEN,
        "x-anila-user-id": runtime_scope.user.username,
        "x-anila-task-id": str(runtime_scope.task.id),
        "x-anila-trace-id": runtime_scope.task.trace_id,
        "x-studio-job-id": runtime_scope.job.job_id,
        "x-studio-artifact-type": runtime_scope.job.artifact_type,
        "x-studio-requester-user-id": str(runtime_scope.user.id),
        "x-studio-collection-id": str(runtime_scope.collection.id),
        "x-studio-task-id": str(runtime_scope.task.id),
        "x-studio-snapshot-id": str(runtime_scope.snapshot.id),
        "x-studio-attempt": "1",
        "x-studio-lease-token": "lease-token-123456789",
    }
    headers = [(key.encode(), value.encode()) for key, value in header_values.items()]
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/studio-runtime/chat/completions",
            "headers": headers,
        }
    )

    async def fake_chat(*, request, caller, db):
        return request.state.prevalidated_task_ctx

    monkeypatch.setattr(studio_runtime, "chat_completions", fake_chat)
    task_ctx = await studio_runtime.runtime_chat_completions(request, binding, db)
    assert task_ctx.task_id == runtime_scope.task.id
    run = db.query(TaskRun).one()
    assert run.task_id == runtime_scope.task.id
    assert run.status == "running"
    assert run.classification_level == runtime_scope.task.classification_level
    assert task_ctx.owns_lifecycle is False
    assert db.query(TaskRun).count() == 1


def test_nested_studio_inference_rejects_non_studio_running_task_run(
    runtime_scope, db
):
    run = db.query(TaskRun).one()
    run.dispatch_target = "model"
    db.commit()

    with pytest.raises(HTTPException) as exc:
        attach_running_task_run(
            db,
            task=runtime_scope.task,
            expected_dispatch_target="studio",
        )

    assert exc.value.status_code == 409
