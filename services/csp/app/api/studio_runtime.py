"""Narrow task/snapshot-bound delegation surface for durable Studio jobs."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.ingestion.image_blob import stream_scoped_image_blob
from app.api.ingestion.search import (
    ImageSearchRequest,
    ImageSearchResponse,
    SearchPrincipal,
    SearchRequest,
    SearchResponse,
    _authorized_document_ids,
    _require_collection_clearance,
    search_collection,
    search_collection_images,
)
from app.api.proxy import _image_generations_impl, chat_completions
from app.database import get_db
from app.middleware.caller import Caller
from app.models.artifact import ArtifactJob
from app.models.ingestion import IngestionCollection
from app.models.registered_service import RegisteredService
from app.models.service_client import ServiceClient
from app.models.source_snapshot import SourceSnapshot
from app.models.task import Task, TaskRun
from app.models.user import User
from app.services import agent_credential_service
from app.services.proxy.task_link import attach_running_task_run
from app.services.studio_runtime_service_bootstrap import (
    CAPABILITY,
    CLIENT_NAME,
    SERVICE_SLUG,
)


router = APIRouter(prefix="/v1/studio-runtime", tags=["Studio Runtime"])
_ARTIFACT_TYPES = {"slides", "report", "mindmap", "infographic", "datatable"}


@dataclass(frozen=True)
class RuntimeBinding:
    user: User
    task: Task
    snapshot: SourceSnapshot
    collection: IngestionCollection
    job: ArtifactJob
    attempt: int
    lease_token: str


def _positive_int(raw: str | None, *, name: str) -> int:
    try:
        value = int((raw or "").strip())
    except (TypeError, ValueError):
        value = 0
    if value < 1:
        raise HTTPException(status_code=400, detail=f"{name} 必須是正整數")
    return value


def require_runtime_binding(
    x_csp_service_token: str | None = Header(None, alias="X-CSP-Service-Token"),
    job_id: str | None = Header(None, alias="X-Studio-Job-Id"),
    artifact_type: str | None = Header(None, alias="X-Studio-Artifact-Type"),
    requester_user_id: str | None = Header(None, alias="X-Studio-Requester-User-Id"),
    studio_collection_id: str | None = Header(
        None, alias="X-Studio-Collection-Id"
    ),
    studio_task_id: str | None = Header(None, alias="X-Studio-Task-Id"),
    canonical_task_id: str | None = Header(None, alias="X-ANILA-Task-Id"),
    studio_snapshot_id: str | None = Header(
        None, alias="X-Studio-Snapshot-Id"
    ),
    trace_id: str | None = Header(None, alias="X-ANILA-Trace-Id"),
    studio_attempt: str | None = Header(None, alias="X-Studio-Attempt"),
    studio_lease_token: str | None = Header(None, alias="X-Studio-Lease-Token"),
    db: Session = Depends(get_db),
) -> RuntimeBinding:
    """Authenticate exact capability and re-authorize canonical data scope."""
    identity = agent_credential_service.verify_service_token(
        db,
        token=(x_csp_service_token or ""),
    )
    if identity is None or identity.kind != "service_client" or identity.is_legacy:
        raise HTTPException(status_code=401, detail="無效或 legacy Studio runtime token")
    client = db.get(ServiceClient, identity.service_client_id)
    services = (
        db.query(RegisteredService)
        .filter(RegisteredService.service_client_id == identity.service_client_id)
        .all()
    )
    if (
        client is None
        or client.client_name != CLIENT_NAME
        or not client.is_active
        or len(services) != 1
        or services[0].slug != SERVICE_SLUG
        or services[0].service_type != "artifact_tool"
        or not services[0].is_active
        or set(services[0].data_egress or []) != {CAPABILITY}
    ):
        raise HTTPException(status_code=403, detail="Studio runtime capability 不符")

    owner_id = _positive_int(requester_user_id, name="requester_user_id")
    collection_pk = _positive_int(studio_collection_id, name="collection_id")
    task_pk = _positive_int(studio_task_id, name="task_id")
    canonical_task_pk = _positive_int(canonical_task_id, name="X-ANILA-Task-Id")
    snapshot_pk = _positive_int(studio_snapshot_id, name="source_snapshot_id")
    attempt = _positive_int(studio_attempt, name="durable attempt")
    lease_token = (studio_lease_token or "").strip()
    if len(lease_token) < 16:
        raise HTTPException(status_code=400, detail="durable lease token 不合法")
    if not job_id or not job_id.strip():
        raise HTTPException(status_code=400, detail="缺少 Studio job id")
    if artifact_type not in _ARTIFACT_TYPES:
        raise HTTPException(status_code=400, detail="artifact_type 不合法")

    user = db.get(User, owner_id)
    task = db.get(Task, task_pk)
    snapshot = db.get(SourceSnapshot, snapshot_pk)
    job = db.get(ArtifactJob, job_id)
    if user is None or not user.is_active or task is None or snapshot is None or job is None:
        raise HTTPException(status_code=404, detail="Studio runtime binding 不存在")
    if (
        task.requester_user_id != user.id
        or canonical_task_pk != task.id
        or task.status != "running"
        or task.source_snapshot_id != snapshot.id
        or snapshot.task_id != task.id
        or collection_pk not in {int(v) for v in (task.selected_collection_ids or [])}
        or collection_pk not in {int(v) for v in (snapshot.collection_ids or [])}
        or job.owner_user_id != user.id
        or job.task_id != task.id
        or job.source_snapshot_id != snapshot.id
        or job.collection_id != collection_pk
        or job.artifact_type != artifact_type
        or job.status not in {"queued", "running"}
        or job.artifact_id is not None
        or (
            task.requested_output_type is not None
            and task.requested_output_type != artifact_type
        )
        or not task.trace_id
        or not job.trace_id
        or not trace_id
        or trace_id != task.trace_id
        or job.trace_id != task.trace_id
        or job.durable_attempt != attempt
        or not job.durable_lease_digest
        or not hmac.compare_digest(
            job.durable_lease_digest,
            hashlib.sha256(lease_token.encode()).hexdigest(),
        )
        or job.expires_at is None
        or (
            job.expires_at.replace(tzinfo=timezone.utc)
            if job.expires_at.tzinfo is None
            else job.expires_at.astimezone(timezone.utc)
        ) <= datetime.now(timezone.utc)
    ):
        raise HTTPException(status_code=403, detail="Studio job/Task/Snapshot scope 不一致")

    principal = SearchPrincipal(user=user, agent=None)
    collection = _require_collection_clearance(
        db,
        principal=principal,
        collection_id=collection_pk,
    )
    if collection.status != "active" or collection.lifecycle_state != "active":
        raise HTTPException(status_code=403, detail="collection 非 active")
    return RuntimeBinding(
        user=user,
        task=task,
        snapshot=snapshot,
        collection=collection,
        job=job,
        attempt=attempt,
        lease_token=lease_token,
    )


def _admit_runtime_sink(db: Session, binding: RuntimeBinding) -> None:
    """Fresh locked admission immediately before a data/inference sink."""
    task = (
        db.query(Task).filter(Task.id == binding.task.id).with_for_update().one()
    )
    job = (
        db.query(ArtifactJob)
        .filter(ArtifactJob.job_id == binding.job.job_id)
        .with_for_update()
        .one()
    )
    running = (
        db.query(TaskRun)
        .filter(TaskRun.task_id == task.id, TaskRun.status == "running")
        .with_for_update()
        .all()
    )
    expires_at = job.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    expected_digest = hashlib.sha256(binding.lease_token.encode()).hexdigest()
    if (
        task.status != "running"
        or job.status not in {"queued", "running"}
        or job.artifact_id is not None
        or len(running) != 1
        or job.durable_attempt != binding.attempt
        or not job.durable_lease_digest
        or not hmac.compare_digest(job.durable_lease_digest, expected_digest)
        or expires_at is None
        or expires_at <= datetime.now(timezone.utc)
    ):
        db.rollback()
        raise HTTPException(status_code=409, detail="Studio runtime sink admission 已失效")
    # Release row locks before retrieval/model I/O; the check itself is the
    # narrow, fresh sink boundary and is repeated for every call.
    db.commit()


def _require_document_scope(
    binding: RuntimeBinding,
    document_ids: list[int] | None,
) -> None:
    if document_ids is None:
        return
    snapshot_ids = {int(value) for value in (binding.snapshot.document_ids or [])}
    if not set(document_ids).issubset(snapshot_ids):
        raise HTTPException(status_code=403, detail="document 不在 canonical snapshot scope")


@router.get("/collections/{collection_id}")
def runtime_collection(
    collection_id: int,
    binding: RuntimeBinding = Depends(require_runtime_binding),
    db: Session = Depends(get_db),
) -> dict:
    _admit_runtime_sink(db, binding)
    if collection_id != binding.collection.id:
        raise HTTPException(status_code=403, detail="collection scope 不符")
    coll = binding.collection
    return {
        "id": coll.id,
        "name": coll.name,
        "embedding_model": coll.embedding_model,
        "embedding_dim": coll.embedding_dim,
        "status": coll.status,
        "created_by": coll.created_by,
    }


@router.post("/collections/{collection_id}/search", response_model=SearchResponse)
async def runtime_search(
    collection_id: int,
    payload: SearchRequest,
    binding: RuntimeBinding = Depends(require_runtime_binding),
    db: Session = Depends(get_db),
) -> SearchResponse:
    _admit_runtime_sink(db, binding)
    if collection_id != binding.collection.id:
        raise HTTPException(status_code=403, detail="collection scope 不符")
    _require_document_scope(binding, payload.document_ids)
    snapshot_document_ids = [
        int(value) for value in (binding.snapshot.document_ids or [])
    ]
    effective_payload = payload.model_copy(
        update={
            "document_ids": (
                payload.document_ids
                if payload.document_ids is not None
                else snapshot_document_ids
            )
        }
    )
    return await search_collection(
        collection_id=collection_id,
        payload=effective_payload,
        db=db,
        principal=SearchPrincipal(user=binding.user, agent=None),
    )


@router.post(
    "/collections/{collection_id}/images/search",
    response_model=ImageSearchResponse,
)
async def runtime_image_search(
    collection_id: int,
    payload: ImageSearchRequest,
    binding: RuntimeBinding = Depends(require_runtime_binding),
    db: Session = Depends(get_db),
) -> ImageSearchResponse:
    _admit_runtime_sink(db, binding)
    if collection_id != binding.collection.id:
        raise HTTPException(status_code=403, detail="collection scope 不符")
    _require_document_scope(binding, payload.document_ids)
    snapshot_docs = [int(value) for value in (binding.snapshot.document_ids or [])]
    effective_payload = payload.model_copy(
        update={
            "document_ids": (
                payload.document_ids
                if payload.document_ids is not None
                else snapshot_docs
            )
        }
    )
    return await search_collection_images(
        collection_id=collection_id,
        payload=effective_payload,
        db=db,
        principal=SearchPrincipal(user=binding.user, agent=None),
    )


@router.get("/images/{image_id}/blob")
def runtime_image_blob(
    image_id: int,
    binding: RuntimeBinding = Depends(require_runtime_binding),
    db: Session = Depends(get_db),
):
    _admit_runtime_sink(db, binding)
    if db.get_bind().dialect.name == "postgresql":
        resolved_collection = db.execute(
            text("SELECT ingestion_image_collection_id(:id)"),
            {"id": image_id},
        ).scalar()
        if resolved_collection is None:
            raise HTTPException(status_code=404, detail="Image not found")
        if int(resolved_collection) != binding.collection.id:
            raise HTTPException(status_code=403, detail="image collection scope 不符")
        db.execute(
            text("SELECT set_config('anila.collection_id', :cid, true)"),
            {"cid": str(binding.collection.id)},
        )
    row = db.execute(
        text(
            "SELECT collection_id, document_id, storage_path, mime "
            "FROM ingestion_images WHERE id=:id"
        ),
        {"id": image_id},
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Image not found")
    snapshot_docs = {int(value) for value in (binding.snapshot.document_ids or [])}
    if int(row.collection_id) != binding.collection.id or int(row.document_id) not in snapshot_docs:
        raise HTTPException(status_code=403, detail="image 不在 canonical snapshot scope")
    _authorized_document_ids(
        db,
        principal=SearchPrincipal(user=binding.user, agent=None),
        collection_id=binding.collection.id,
        requested_ids=[int(row.document_id)],
        reject_denied=True,
    )
    return stream_scoped_image_blob(
        image_id=image_id,
        storage_path=str(row.storage_path),
        mime=(str(row.mime) if row.mime else None),
    )


@router.post("/chat/completions")
async def runtime_chat_completions(
    request: Request,
    binding: RuntimeBinding = Depends(require_runtime_binding),
    db: Session = Depends(get_db),
):
    _admit_runtime_sink(db, binding)
    # Attach the nested model call to the one already-running outer artifact
    # run.  The proxy records usage/spans but must not finish the outer Task.
    request.state.prevalidated_task_ctx = attach_running_task_run(
        db,
        task=binding.task,
        expected_dispatch_target="studio",
    )
    return await chat_completions(
        request=request,
        caller=Caller(user=binding.user, api_key_id=None),
        db=db,
    )


@router.post("/images/generations")
async def runtime_image_generations(
    request: Request,
    binding: RuntimeBinding = Depends(require_runtime_binding),
    db: Session = Depends(get_db),
):
    """Task/snapshot/lease-bound Studio entry into the governed Images proxy."""

    _admit_runtime_sink(db, binding)
    request.state.prevalidated_task_ctx = attach_running_task_run(
        db,
        task=binding.task,
        expected_dispatch_target="studio",
    )
    return await _image_generations_impl(
        request,
        caller=Caller(user=binding.user, api_key_id=None),
        db=db,
    )


__all__ = ["RuntimeBinding", "require_runtime_binding", "router"]
