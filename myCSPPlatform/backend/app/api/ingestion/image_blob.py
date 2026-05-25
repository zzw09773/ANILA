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
from app.database import get_db
from app.models.user import User
from app.services.auth_service import get_current_user

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
    row = db.execute(
        text(
            """
            SELECT id, collection_id, storage_path, mime
            FROM ingestion_images
            WHERE id = :id
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
    storage_path = str(row.storage_path)
    mime = str(row.mime) if row.mime else "image/png"

    # Authorise via the parent collection. Raises 403/404 from inside.
    _require_collection_access(db, current_user, collection_id)

    upload_root = os.environ.get(
        "INGESTION_UPLOAD_DIR", "/var/anila/ingestion-uploads",
    )
    # ``storage_path`` is the relative path the worker recorded; join
    # against the configured upload root to obtain the absolute file.
    abs_path = Path(upload_root) / storage_path
    if not abs_path.is_file():
        logger.warning(
            "Image %s storage_path=%s missing under upload root %s",
            image_id, storage_path, upload_root,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Image {image_id} blob unavailable",
        )

    return StreamingResponse(
        _stream_file(abs_path),
        media_type=mime,
    )
