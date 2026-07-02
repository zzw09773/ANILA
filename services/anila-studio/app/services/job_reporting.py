"""anila-studio → CSP artifact-job / artifact reporter (Slice 8b).

The parallel worker 8a builds the CSP-side wire contract; studio codes
against it (and the tests mock it with respx):

    POST  {csp}/v1/artifact-jobs                  → 201 (idempotent on job_id)
        {job_id, artifact_type, task_id?, source_snapshot_id?,
         requester, trace_id?, status, ...}
    PATCH {csp}/v1/artifact-jobs/{job_id}         → 200
        {status, error?, progress?, artifact_id?}
    POST  {csp}/v1/artifacts                       → 201
        {job_id?, task_id?, source_snapshot_id?, artifact_type, title,
         storage_ref, content_hash?, classification_level?, metadata?}
      ← {artifact_id, version_id, classification_level}

Auth
====
Reuse studio's existing CSP auth: the user's bearer JWT
(``Authorization: Bearer``), identical to every ``csp_client`` call — CSP
re-verifies it (RS256 + JWKS) so user-on-behalf-of semantics hold. When a
legacy ``CSP_SERVICE_TOKEN`` is configured we additionally attach
``X-CSP-Service-Token`` (mirroring the revocation cold-start path), so 8a
may accept either credential.

Resilience
==========
Every call is fire-and-forget: retry once on transport / 5xx, then give
up; ALL errors are swallowed + logged. CSP being down must never break
generation. Does NOT import ``anila_core``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.config import settings


logger = logging.getLogger(__name__)


# One retry on top of the initial attempt (2 total). A short fixed backoff —
# unlike csp_client we never want to burn a long budget here, this is
# best-effort bookkeeping alongside a job that already ran.
_RETRY_DELAY_SECONDS = 0.5
_TIMEOUT = httpx.Timeout(
    timeout=settings.INTERNAL_TIMEOUT_SECONDS,
    connect=settings.INTERNAL_TIMEOUT_CONNECT,
)


def _headers(bearer: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if settings.CSP_SERVICE_TOKEN:
        headers["X-CSP-Service-Token"] = settings.CSP_SERVICE_TOKEN
    return headers


async def _send(
    method: str,
    url: str,
    *,
    bearer: str | None,
    json_body: dict[str, Any],
) -> httpx.Response | None:
    """Issue one request, retrying once on transport / 5xx errors.

    Returns the response, or ``None`` when the call could not be completed
    (all failure modes swallowed). NEVER raises.
    """
    headers = _headers(bearer)
    for attempt in range(2):  # initial + one retry
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.request(method, url, headers=headers, json=json_body)
            if resp.status_code >= 500 and attempt == 0:
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
                continue
            return resp
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
        ) as exc:
            if attempt == 0:
                logger.debug("artifact reporter transport error on %s %s: %s", method, url, exc)
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
                continue
            logger.warning("artifact reporter gave up on %s %s: %s", method, url, exc)
            return None
        except Exception as exc:  # noqa: BLE001 — never propagate
            logger.warning("artifact reporter unexpected error on %s %s: %s", method, url, exc)
            return None
    return None


async def report_job_created(
    *,
    bearer: str | None,
    job_id: str,
    artifact_type: str,
    status: str,
    requester: str | None = None,
    task_id: str | None = None,
    source_snapshot_id: str | None = None,
    trace_id: str | None = None,
) -> None:
    """POST /v1/artifact-jobs — register the job on the control plane."""
    body: dict[str, Any] = {
        "job_id": job_id,
        "artifact_type": artifact_type,
        "status": status,
    }
    if requester is not None:
        body["requester"] = requester
    if task_id is not None:
        body["task_id"] = task_id
    if source_snapshot_id is not None:
        body["source_snapshot_id"] = source_snapshot_id
    if trace_id is not None:
        body["trace_id"] = trace_id
    url = f"{settings.CSP_BASE_URL}/v1/artifact-jobs"
    resp = await _send("POST", url, bearer=bearer, json_body=body)
    if resp is not None and resp.status_code >= 400:
        logger.warning(
            "artifact-job create for %s rejected: HTTP %s", job_id, resp.status_code
        )


async def report_job_patched(
    *,
    bearer: str | None,
    job_id: str,
    status: str,
    error: str | None = None,
    progress: float | None = None,
    artifact_id: str | None = None,
) -> None:
    """PATCH /v1/artifact-jobs/{job_id} — push a terminal / progress update."""
    body: dict[str, Any] = {"status": status}
    if error is not None:
        body["error"] = error
    if progress is not None:
        body["progress"] = progress
    if artifact_id is not None:
        body["artifact_id"] = artifact_id
    url = f"{settings.CSP_BASE_URL}/v1/artifact-jobs/{job_id}"
    resp = await _send("PATCH", url, bearer=bearer, json_body=body)
    if resp is not None and resp.status_code >= 400:
        logger.warning(
            "artifact-job patch for %s rejected: HTTP %s", job_id, resp.status_code
        )


async def register_artifact(
    *,
    bearer: str | None,
    artifact_type: str,
    title: str | None,
    storage_ref: str,
    job_id: str | None = None,
    task_id: str | None = None,
    source_snapshot_id: str | None = None,
    content_hash: str | None = None,
    classification_level: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """POST /v1/artifacts — persist the produced artifact + version.

    Returns the parsed ``{artifact_id, version_id, classification_level}``
    dict on success, or ``None`` when the call could not complete /
    returned a non-2xx (swallowed).
    """
    body: dict[str, Any] = {
        "artifact_type": artifact_type,
        "title": title,
        "storage_ref": storage_ref,
    }
    if job_id is not None:
        body["job_id"] = job_id
    if task_id is not None:
        body["task_id"] = task_id
    if source_snapshot_id is not None:
        body["source_snapshot_id"] = source_snapshot_id
    if content_hash is not None:
        body["content_hash"] = content_hash
    if classification_level is not None:
        body["classification_level"] = classification_level
    if metadata:
        body["metadata"] = metadata
    url = f"{settings.CSP_BASE_URL}/v1/artifacts"
    resp = await _send("POST", url, bearer=bearer, json_body=body)
    if resp is None:
        return None
    if resp.status_code >= 400:
        logger.warning(
            "artifact register for job=%s rejected: HTTP %s", job_id, resp.status_code
        )
        return None
    try:
        data = resp.json()
    except ValueError:
        logger.warning("artifact register for job=%s returned non-JSON body", job_id)
        return None
    return data if isinstance(data, dict) else None
