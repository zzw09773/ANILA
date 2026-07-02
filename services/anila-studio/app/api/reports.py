"""Studio Report (deep report) API — job-based async pipeline.

Mirrors the slides endpoint contract (``/api/studio/slides/jobs``) but
for hierarchical text reports rendered to HTML/PDF/DOCX.

    POST   /api/reports/jobs                          → 202 + ReportJobStatus
    GET    /api/reports/jobs/{id}                     → ReportJobStatus
    GET    /api/reports/jobs/{id}/download/{fmt}      → binary stream
    DELETE /api/reports/jobs/{id}                     → 204

The router is NOT registered in ``main.py`` yet — wave-Z does that to
keep parallel feature subagents from stepping on the entrypoint.
Importing this module is side-effect free aside from the router
construction.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import StreamingResponse

from app.auth import (
    CurrentUserIdentity,
    get_bearer_token,
    get_current_user_identity,
)
from app.clients.csp_client import (
    CspClientError,
    CspForbiddenError,
    CspNotFoundError,
    CspServerError,
    CspUnauthorizedError,
    get_collection,
)
from app.config import settings
from app.schemas.report import (
    GenerateReportRequest,
    ReportJobStatus,
)
from app.services import job_lifecycle
from app.services import report_job_service as jobs
from app.services.report_runner import run_report_pipeline


router = APIRouter(prefix="/api/reports", tags=["Studio / Reports"])
logger = logging.getLogger(__name__)


# ── Format → MIME / extension map ────────────────────────────────────────


_FMT_MIME: dict[str, str] = {
    "html": "text/html; charset=utf-8",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.post(
    "/jobs",
    response_model=ReportJobStatus,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_report_job(
    payload: GenerateReportRequest,
    identity: CurrentUserIdentity = Depends(get_current_user_identity),
    bearer: str = Depends(get_bearer_token),
) -> ReportJobStatus:
    """Register a report generation job and return its initial status.

    Returns 202 with state="pending" and a job_id. The pipeline runs in
    an asyncio.Task. The client polls GET /jobs/{id} until state="done"
    then downloads any of the three formats.

    Collection auth is validated synchronously up-front so the user gets
    a 403/404 immediately on bad input, not as a delayed "failed" job.
    """
    try:
        await get_collection(payload.collection_id, bearer=bearer)
    except CspNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CspForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except CspUnauthorizedError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except (CspServerError, CspClientError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    async def _runner(updater: jobs.ReportJobUpdater) -> None:
        await run_report_pipeline(
            request=payload,
            bearer=bearer,
            updater=updater,
        )

    report_ctx = job_lifecycle.make_context(
        artifact_type="report",
        owner_user_id=identity.id,
        requester=identity.username or str(identity.id),
        bearer=bearer,
        collection_id=payload.collection_id,
        describe=jobs.artifact_info,
        task_id=payload.task_id,
        source_snapshot_id=payload.source_snapshot_id,
        trace_id=payload.trace_id,
    )
    record = await jobs.create_job(
        user_id=identity.id,
        collection_id=payload.collection_id,
        preset=payload.preset,
        runner=_runner,
        report_ctx=report_ctx,
    )
    return record.to_status()


@router.get("/jobs/{job_id}", response_model=ReportJobStatus)
async def get_report_job(
    job_id: str,
    identity: CurrentUserIdentity = Depends(get_current_user_identity),
) -> ReportJobStatus | dict:
    """Cheap polling endpoint. Returns the current status or 404.

    404 covers both "doesn't exist" and "exists but belongs to someone
    else" — never leak cross-user existence. Read-through to the durable
    job store when the in-memory record is gone (studio restart / eviction).
    """
    rec = jobs.get_user_job(job_id, identity.id)
    if rec is not None:
        return rec.to_status()
    persisted = await job_lifecycle.read_status(job_id, identity.id)
    if persisted is not None:
        return persisted
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Report job not found (unknown id, evicted, or not yours).",
    )


@router.get(
    "/jobs/{job_id}/download/{fmt}",
    responses={
        200: {"content": {m: {} for m in _FMT_MIME.values()}},
    },
)
async def download_report(
    job_id: str,
    fmt: Literal["html", "pdf", "docx"],
    identity: CurrentUserIdentity = Depends(get_current_user_identity),
) -> StreamingResponse:
    """Stream the rendered artifact for a completed job.

    State semantics mirror the slides endpoint:
      - 404: unknown id / not yours
      - 409: still pending or running
      - 410: failed or cancelled
      - 200: streams the file from ``{ARTIFACTS_DIR}/{job_id}.{fmt}``

    The actual bytes live on disk (reports are too big to keep in-RAM
    like the slide pptx_bytes — a typical PDF can hit several MB).
    """
    rec = jobs.get_user_job(job_id, identity.id)
    if rec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Report job not found."
        )
    if rec.state in ("pending", "running"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Report not ready yet (state={rec.state}).",
        )
    if rec.state in ("failed", "cancelled"):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=f"Report job is {rec.state}: {rec.error or '(no detail)'}",
        )
    if rec.download_urls is None or fmt not in rec.download_urls:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Report marked done but {fmt} URL is missing.",
        )

    path = Path(settings.ARTIFACTS_DIR) / f"{job_id}.{fmt}"
    if not path.exists() or not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=f"Report artifact missing on disk: {fmt}",
        )

    # Filename: percent-encode for RFC 5987 to handle CJK titles cleanly.
    from urllib.parse import quote

    raw_title = (rec.title or "report").replace('"', "")[:80]
    encoded_title = quote(raw_title, safe="")
    ascii_title = (
        raw_title.encode("ascii", "ignore").decode("ascii").strip() or "report"
    )

    file_size = path.stat().st_size
    # Read in chunks rather than slurping the whole file — DOCX/PDF can
    # be several MB and streaming is friendly to slow clients.
    chunk_size = 64 * 1024

    async def _iter() -> AsyncIterator[bytes]:
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                yield chunk

    mime = _FMT_MIME[fmt]
    return StreamingResponse(
        _iter(),
        media_type=mime,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_title}.{fmt}"; '
                f"filename*=UTF-8''{encoded_title}.{fmt}"
            ),
            "Content-Length": str(file_size),
        },
    )


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_report_job(
    job_id: str,
    identity: CurrentUserIdentity = Depends(get_current_user_identity),
) -> Response:
    """Cancel an in-flight report job. No-op if already terminal.

    Returns 204 on success or already-terminal; 404 if the job doesn't
    belong to the caller (or never existed).
    """
    cancelled = await jobs.cancel_job(job_id, identity.id)
    if not cancelled:
        rec = jobs.get_user_job(job_id, identity.id)
        if rec is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Report job not found.",
            )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
