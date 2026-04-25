"""Ingestion documents endpoint group.

Sprint 1 ships:

- ``POST /api/ingestion/collections/{id}/documents`` — multipart upload,
  writes blob to UPLOAD_DIR, INSERTs ingestion_documents row, enqueues
  ingest_document Arq job, INSERTs ingestion_jobs row tied to the job id.
- ``GET  /api/ingestion/collections/{id}/documents`` — paginated list of
  documents in a collection.
- ``GET  /api/ingestion/documents/{id}`` — detail row + last job row.

Document upload is the API end of the pipeline; the worker takes over
from the moment we enqueue. The dev sees the document in 'pending'
status immediately, then 'parsing' / 'chunking' / 'embedding' / 'indexed'
as the worker advances.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timezone

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.ingestion.collections import _require_agent_access
from app.database import get_db
from app.models.ingestion import (
    IngestionCollection,
    IngestionDocument,
    IngestionJob,
)
from app.models.user import User
from app.services.audit_service import log_audit_event
from app.services.auth_service import get_current_user
from app.services.ingestion_queue import enqueue_ingest_document

router = APIRouter(tags=["Ingestion / Documents"])


_UPLOAD_DIR = os.environ.get("INGESTION_UPLOAD_DIR", "/var/anila/ingestion-uploads")

# Sprint 1 hard cap. Larger files are a Sprint 2 concern (chunked upload,
# resumable, progress) — for now hard-fail with 413.
_MAX_BYTES = 50 * 1024 * 1024  # 50 MB


class DocumentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    collection_id: int
    filename: str
    sha256: str
    mime_type: str | None
    bytes: int | None
    status: str
    chunk_count: int
    error_message: str | None
    uploaded_by: int | None
    uploaded_at: datetime
    indexed_at: datetime | None


class DocumentDetailResponse(DocumentResponse):
    """Document row + last job row for status display."""

    latest_job_id: int | None = None
    latest_job_status: str | None = None
    latest_job_error_code: str | None = None
    arq_job_id: str | None = None


# ── Helpers ─────────────────────────────────────────────────────────────────


def _resolve_collection(
    db: Session, user: User, collection_id: int
) -> IngestionCollection:
    coll = (
        db.query(IngestionCollection)
        .filter(IngestionCollection.id == collection_id)
        .first()
    )
    if coll is None:
        raise HTTPException(status_code=404, detail="Collection not found")
    _require_agent_access(db, user, coll.agent_id)
    return coll


def _persist_blob(content: bytes, sha256: str) -> str:
    """Write the upload to disk under a content-addressable path.

    Path = ``UPLOAD_DIR/<sha256[:2]>/<sha256>`` so we get a flat 2-deep
    directory structure (~256 entries per top-level dir even at scale).
    Same sha256 → same path → re-uploads are no-ops at the FS layer.
    The DB layer separately enforces the (collection_id, sha256) unique.
    """
    sub = os.path.join(_UPLOAD_DIR, sha256[:2])
    os.makedirs(sub, exist_ok=True)
    path = os.path.join(sub, sha256)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(content)
    return path


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.post(
    "/api/ingestion/collections/{collection_id}/documents",
    response_model=DocumentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_document(
    collection_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DocumentResponse:
    """Accept one file, persist, enqueue ingestion job.

    Returns 202 (Accepted) — the document row is written but indexing
    happens async. Caller polls ``GET /api/ingestion/documents/{id}``
    to watch status transitions.
    """
    coll = _resolve_collection(db, current_user, collection_id)

    # Read fully into memory — Sprint 1 caps uploads at 50 MB so this is
    # fine; Sprint 2 streaming upload will spool to disk in chunks.
    content = await file.read()
    size = len(content)
    if size == 0:
        raise HTTPException(status_code=400, detail="Empty file")
    if size > _MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({size:,} > {_MAX_BYTES:,} bytes)",
        )

    sha256 = hashlib.sha256(content).hexdigest()
    storage_path = _persist_blob(content, sha256)

    # Insert the document row. Uniqueness on (collection_id, sha256) gives
    # us cheap content-level dedup — re-uploading the same file just
    # returns the existing row.
    doc = IngestionDocument(
        collection_id=collection_id,
        filename=file.filename or sha256,
        sha256=sha256,
        mime_type=file.content_type,
        bytes=size,
        storage_path=storage_path,
        status="pending",
        chunk_count=0,
        uploaded_by=current_user.id,
    )
    db.add(doc)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Same content already uploaded to this collection — return the
        # existing row instead of erroring. Idempotent uploads matter
        # for retry-prone clients.
        existing = (
            db.query(IngestionDocument)
            .filter(
                IngestionDocument.collection_id == collection_id,
                IngestionDocument.sha256 == sha256,
            )
            .first()
        )
        if existing is None:
            raise HTTPException(status_code=500, detail="Upload conflict")
        return DocumentResponse.model_validate(existing)
    db.refresh(doc)

    # Enqueue + create the matching jobs row. We do this in two steps
    # because Arq returns a job id only after enqueue, and we want the
    # row to carry that id from the start (no UPDATE-after-INSERT race).
    arq_job_id = await enqueue_ingest_document(doc.id)
    job = IngestionJob(
        arq_job_id=arq_job_id,
        collection_id=collection_id,
        document_id=doc.id,
        job_type="ingest",
        status="queued",
        progress_pct=0,
        enqueued_by=current_user.id,
    )
    db.add(job)
    db.commit()

    log_audit_event(
        db,
        actor=current_user,
        action="ingestion_document_upload",
        resource_type="ingestion_document",
        resource_id=doc.id,
        metadata={
            "collection_id": collection_id,
            "filename": doc.filename,
            "size": size,
            "arq_job_id": arq_job_id,
        },
    )
    return DocumentResponse.model_validate(doc)


@router.get(
    "/api/ingestion/collections/{collection_id}/documents",
    response_model=list[DocumentResponse],
)
def list_documents(
    collection_id: int,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[DocumentResponse]:
    _resolve_collection(db, current_user, collection_id)
    rows = (
        db.query(IngestionDocument)
        .filter(IngestionDocument.collection_id == collection_id)
        .order_by(IngestionDocument.id.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )
    return [DocumentResponse.model_validate(r) for r in rows]


@router.get(
    "/api/ingestion/documents/{document_id}",
    response_model=DocumentDetailResponse,
)
def get_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DocumentDetailResponse:
    """Document row + most recent job row joined.

    The inspector polls this endpoint to render the parse → chunk →
    embed → indexed timeline. We always fetch the *latest* job because
    a re-ingest creates a new row; the older ones stay for audit but
    aren't UI-relevant.
    """
    doc = (
        db.query(IngestionDocument)
        .filter(IngestionDocument.id == document_id)
        .first()
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    coll = _resolve_collection(db, current_user, doc.collection_id)  # auth + 404
    _ = coll  # only invoked for its side-effect (auth check).

    latest_job = (
        db.query(IngestionJob)
        .filter(IngestionJob.document_id == document_id)
        .order_by(IngestionJob.id.desc())
        .first()
    )

    payload = DocumentDetailResponse.model_validate(doc)
    if latest_job is not None:
        payload.latest_job_id = latest_job.id
        payload.latest_job_status = latest_job.status
        payload.latest_job_error_code = latest_job.error_code
        payload.arq_job_id = latest_job.arq_job_id
    return payload
