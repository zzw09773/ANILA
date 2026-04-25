"""Ingestion job status SSE stream.

``GET /api/ingestion/jobs/{job_id}/stream`` keeps a long-lived
``text/event-stream`` connection open and emits one frame whenever the
job's row in ``ingestion_jobs`` changes. The stream closes naturally
once the job reaches a terminal state (``succeeded`` / ``failed`` /
``cancelled``).

Frame shape:

```
data: {"status":"chunking","progress_pct":40,"progress_message":null,"error_code":null}

```

We poll the DB every ``_POLL_SECONDS``. PG is cheap for a single
indexed SELECT (job_id is unique-indexed); the worker writes status
transitions inside its own transactions so polling sees consistent
snapshots. Switching to LISTEN/NOTIFY is a Sprint-3+ optimisation.

Authorisation: caller must have access to the agent owning the job's
collection — same gate the rest of ``/api/ingestion/*`` uses.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.ingestion.collections import _require_agent_access
from app.database import SessionLocal, get_db
from app.models.ingestion import IngestionCollection, IngestionJob
from app.models.user import User
from app.services.auth_service import get_current_user


router = APIRouter(tags=["Ingestion / Jobs"])


# Polling cadence. 1 second is a reasonable trade-off between perceived
# UI snap and DB load — typical ingest pipeline emits state transitions
# every few seconds (parsing / chunking / embedding / indexed). 15s
# heartbeat keeps proxies (nginx default 60s read timeout) from dropping
# the connection during long embedding waits.
_POLL_SECONDS = 1.0
_HEARTBEAT_SECONDS = 15.0
_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
# Hard cap so a stuck job doesn't keep an open connection forever.
_MAX_STREAM_SECONDS = 30 * 60  # 30 minutes


def _frame(payload: dict) -> bytes:
    """Encode one SSE data frame. The trailing blank line is the
    delimiter the EventSource spec requires."""
    return (
        f"data: {json.dumps(payload, default=_json_default)}\n\n"
    ).encode("utf-8")


def _json_default(o):
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"not serialisable: {type(o).__name__}")


def _job_snapshot(job: IngestionJob) -> dict:
    """Public projection of one job row — what the frontend renders."""
    return {
        "id": job.id,
        "status": job.status,
        "progress_pct": job.progress_pct,
        "progress_message": job.progress_message,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
    }


async def _stream(job_id: int, agent_id: int) -> AsyncIterator[bytes]:
    """Async generator: yields SSE frames until the job terminates.

    Opens a fresh SQLAlchemy Session per poll because we don't want a
    connection pinned for the lifetime of the stream — that would
    exhaust the pool fast under more than a handful of concurrent
    inspector clients. PG handles the per-second SELECT easily; the
    job_id index makes it cheap.
    """
    last_signature: tuple | None = None
    last_heartbeat = asyncio.get_event_loop().time()
    deadline = last_heartbeat + _MAX_STREAM_SECONDS

    while True:
        now = asyncio.get_event_loop().time()
        if now >= deadline:
            yield _frame({"event": "stream_timeout"})
            return

        # Per-iteration session — open / close cleanly so the connection
        # returns to the pool between polls.
        db = SessionLocal()
        try:
            job = db.execute(
                select(IngestionJob).where(IngestionJob.id == job_id)
            ).scalar_one_or_none()
            if job is None:
                yield _frame({"event": "job_not_found", "id": job_id})
                return
            snap = _job_snapshot(job)
        finally:
            db.close()

        # Emit only on change to keep frontend re-render quiet.
        signature = (
            snap["status"],
            snap["progress_pct"],
            snap["progress_message"],
            snap["error_code"],
        )
        if signature != last_signature:
            yield _frame(snap)
            last_signature = signature

        if snap["status"] in _TERMINAL_STATUSES:
            return

        # Heartbeat path — comment frame, no JSON. Keeps the proxy /
        # browser EventSource connection alive on long embedding waits
        # without churning the UI handler.
        if now - last_heartbeat >= _HEARTBEAT_SECONDS:
            yield b": keepalive\n\n"
            last_heartbeat = now

        await asyncio.sleep(_POLL_SECONDS)


@router.get("/api/ingestion/jobs/{job_id}/stream")
async def stream_job(
    job_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    """SSE stream of one job's status transitions."""
    job = (
        db.query(IngestionJob).filter(IngestionJob.id == job_id).first()
    )
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found",
        )

    coll = (
        db.query(IngestionCollection)
        .filter(IngestionCollection.id == job.collection_id)
        .first()
    )
    if coll is None:
        # Orphan job — its collection was deleted. Still 404 for the
        # caller because the audit story is "job is no longer accessible".
        raise HTTPException(status_code=404, detail="Job's collection is gone")
    _require_agent_access(db, current_user, coll.agent_id)

    return StreamingResponse(
        _stream(job_id, coll.agent_id),
        media_type="text/event-stream",
        # Disable buffering at the proxy / nginx layer for SSE.
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
