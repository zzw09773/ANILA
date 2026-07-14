"""Stream raw bytes for an ``ingestion_images`` row.

Phase 1 of the anila-studio extraction: studio downloads original PDF
images via HTTP rather than reading the csp-db share volume directly.
Using a ``StreamingResponse`` (not base64-in-JSON) keeps RAM bounded
on multi-megabyte PDF rasters — buffering an entire deck's worth of
images into JSON would balloon the response payload and the
recipient's heap.

Auth pattern mirrors the search endpoint: load the row → resolve the
parent collection → ``_require_collection_access`` (admin or owner).

The ``ingestion_images`` table is created via raw alembic DDL
(migration 0026) and has no SQLAlchemy model class — we use a
``text()`` lookup so the same query runs on both production Postgres
and the SQLite test fixture without dragging in a halfvec-typed column.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.ingestion.collections import _require_collection_access
from app.api.ingestion.documents import _require_document_data_clearance
from app.database import get_db
from app.models.ingestion import IngestionDocument
from app.models.user import User
from app.services.auth_service import get_current_user
from app.services.retention_reaper import RetentionSafetyError, safe_ingestion_path

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Ingestion / Images"])


# Streaming chunk size — 64 KiB tracks the typical filesystem read-ahead
# window. Smaller risks too many syscalls; larger forces extra copies
# inside Starlette's send buffer without latency benefit.
_STREAM_CHUNK = 64 * 1024


def _stream_file(path: Path) -> AsyncIterator[bytes]:
    """Yield the file contents in ``_STREAM_CHUNK``-sized chunks.

    Defined as a sync generator that Starlette awaits — keeps the
    interface portable across async / sync file backends. Switching to
    object storage later only requires changing this helper.
    """

    def _generator():
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(_STREAM_CHUNK)
                if not chunk:
                    break
                yield chunk

    return _generator()


def stream_scoped_image_blob(
    *,
    image_id: int,
    storage_path: str,
    mime: str | None,
) -> StreamingResponse:
    """Stream bytes after the caller has performed canonical live auth."""
    upload_root = os.environ.get(
        "INGESTION_UPLOAD_DIR", "/var/anila/ingestion-uploads",
    )
    try:
        abs_path = safe_ingestion_path(upload_root, storage_path)
    except RetentionSafetyError:
        logger.error("Rejecting unsafe image storage_path for image %s", image_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Image {image_id} blob unavailable",
        ) from None
    if not abs_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Image {image_id} blob unavailable",
        )
    return StreamingResponse(
        _stream_file(abs_path),
        media_type=mime or "image/png",
    )


@router.get("/api/ingestion/images/{image_id}/blob")
def get_image_blob(
    image_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    """Return raw image bytes for an ``ingestion_images.id``.

    Args:
        image_id: ``ingestion_images.id`` (BIGSERIAL primary key). The
            TEXT ``image_id`` column is per-document and not globally
            unique, so the API addresses rows by PK.

    Raises:
        404: row not found.
        403: caller does not own the parent collection.
        404 (again): the row exists but ``storage_path`` resolves to a
            file that's missing from the upload root — treat as "image
            unavailable" because exposing a 500 to anila-studio would
            be alarming for a deterministic infra mismatch we can't
            recover from at the HTTP layer.
    """
    # ingestion_images is FORCE-RLS on Postgres (migration 0037). This is a
    # by-PK lookup, so we can't read the row to learn its collection before the
    # GUC is set (chicken-and-egg). On Postgres: resolve the collection via the
    # SECURITY DEFINER helper (returns only collection_id, bypassing RLS),
    # enforce ownership, set the txn-local GUC, then read the sensitive columns
    # under RLS. RLS is a Postgres feature; on other backends (e.g. the SQLite
    # test DB) there is no row security, so a single scoped read is correct.
    is_postgres = db.get_bind().dialect.name == "postgresql"
    if is_postgres:
        collection_id = db.execute(
            text("SELECT ingestion_image_collection_id(:id)"),
            {"id": image_id},
        ).scalar()
        if collection_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Image {image_id} not found",
            )
        collection_id = int(collection_id)

        # Real access gate (RLS below is defence-in-depth). Raises 403/404.
        _require_collection_access(db, current_user, collection_id)

        # Scope the connection, then read the row under RLS. set_config(...,
        # is_local => true) is txn-scoped → never leaks to the next pooled user.
        db.execute(
            text("SELECT set_config('anila.collection_id', :cid, true)"),
            {"cid": str(collection_id)},
        )
        row = db.execute(
            text(
                "SELECT i.document_id, i.storage_path, i.mime FROM ingestion_images i "
                "JOIN ingestion_documents d ON d.id=i.document_id "
                "WHERE i.id=:id AND d.lifecycle_state='active'"
            ),
            {"id": image_id},
        ).first()
        if row is None:
            # Resolver found it but the RLS-scoped read didn't — guard the edge.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Image {image_id} blob unavailable",
            )
        storage_path = str(row.storage_path)
        mime = str(row.mime) if row.mime else "image/png"
    else:
        row = db.execute(
            text(
                """
                SELECT i.collection_id, i.document_id, i.storage_path, i.mime
                FROM ingestion_images i
                JOIN ingestion_documents d ON d.id=i.document_id
                WHERE i.id = :id AND d.lifecycle_state='active'
                """
            ),
            {"id": image_id},
        ).first()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Image {image_id} not found",
            )
        collection_id = int(row.collection_id)
        _require_collection_access(db, current_user, collection_id)
        storage_path = str(row.storage_path)
        mime = str(row.mime) if row.mime else "image/png"

    document = db.get(IngestionDocument, int(row.document_id))
    if document is None or document.collection_id != collection_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Image {image_id} blob unavailable",
        )
    _require_document_data_clearance(
        db, user=current_user, document=document,
    )

    return stream_scoped_image_blob(
        image_id=image_id,
        storage_path=storage_path,
        mime=mime,
    )
