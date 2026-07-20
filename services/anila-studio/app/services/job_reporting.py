"""Strict anila-studio → CSP artifact control-plane reporter.

Artifact writes use a dedicated named, non-legacy Service Client token. User
bearer tokens are deliberately not forwarded to this service-only boundary;
the requester identity and Task/Snapshot binding travel in the validated body.
Failures remain observable through Studio readiness and terminal registration
fails the local job closed instead of presenting an unregistered artifact as
completed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import settings


logger = logging.getLogger(__name__)

_RETRY_DELAY_SECONDS = 0.5
_TIMEOUT = httpx.Timeout(
    timeout=settings.INTERNAL_TIMEOUT_SECONDS,
    connect=settings.INTERNAL_TIMEOUT_CONNECT,
)


class ArtifactReportingError(RuntimeError):
    """CSP rejected or could not receive an artifact control-plane write."""


@dataclass
class _ReportingStatus:
    ready: bool = False
    last_success_at: str | None = None
    last_failure_at: str | None = None
    last_error: str | None = None
    consecutive_failures: int = 0


_status = _ReportingStatus()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_success() -> None:
    _status.ready = True
    _status.last_success_at = _now()
    _status.last_error = None
    _status.consecutive_failures = 0


def _record_failure(message: str) -> None:
    _status.ready = False
    _status.last_failure_at = _now()
    _status.last_error = message[:500]
    _status.consecutive_failures += 1


def reporting_status() -> dict[str, Any]:
    """Return a secret-free readiness projection for ``/health``."""
    if not settings.STUDIO_ARTIFACT_REPORTING:
        return {"ready": True, "enabled": False}
    body = asdict(_status)
    body["enabled"] = True
    body["configured"] = bool(settings.STUDIO_ARTIFACT_SERVICE_TOKEN)
    return body


def reset_reporting_status() -> None:
    """Reset process-local status; intended for deterministic tests."""
    global _status
    _status = _ReportingStatus()


def mark_reporting_failure(message: str) -> None:
    """Keep readiness degraded after a compensating status write succeeds."""
    _record_failure(message)


def _headers() -> dict[str, str]:
    token = settings.STUDIO_ARTIFACT_SERVICE_TOKEN.strip()
    if not token:
        raise ArtifactReportingError(
            "STUDIO_ARTIFACT_SERVICE_TOKEN 缺失;不得回退 legacy/browser token"
        )
    return {"X-CSP-Service-Token": token}


def _binding_id(value: str | int | None, *, field: str) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        message = f"{field} 必須是正整數"
        _record_failure(message)
        raise ArtifactReportingError(message) from exc
    if parsed <= 0:
        message = f"{field} 必須是正整數"
        _record_failure(message)
        raise ArtifactReportingError(message)
    return parsed


async def _send(
    method: str,
    url: str,
    *,
    json_body: dict[str, Any] | None = None,
    data: dict[str, str] | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
    allowed_statuses: frozenset[int] = frozenset(),
    extra_headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Send a strict request, retrying transport/5xx once and then raising."""
    try:
        headers = _headers()
        headers.update(extra_headers or {})
    except ArtifactReportingError as exc:
        _record_failure(str(exc))
        raise

    last_error = "unknown artifact reporting error"
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    data=data,
                    files=files,
                )
            if response.status_code >= 500 and attempt == 0:
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
                continue
            if response.status_code >= 400 and response.status_code not in allowed_statuses:
                last_error = f"{method} {url} rejected: HTTP {response.status_code}"
                _record_failure(last_error)
                raise ArtifactReportingError(last_error)
            _record_success()
            return response
        except ArtifactReportingError:
            raise
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
        ) as exc:
            last_error = f"{method} {url} transport error: {type(exc).__name__}"
            if attempt == 0:
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
                continue
            _record_failure(last_error)
            raise ArtifactReportingError(last_error) from exc
    _record_failure(last_error)
    raise ArtifactReportingError(last_error)


async def probe_readiness() -> bool:
    """Validate the named token and exact registry capability without writes."""
    if not settings.STUDIO_ARTIFACT_REPORTING:
        _record_success()
        return True
    url = f"{settings.CSP_BASE_URL}/v1/artifact-writer/ready"
    try:
        response = await _send("GET", url)
        body = response.json()
        valid = (
            isinstance(body, dict)
            and body.get("ready") is True
            and body.get("service_type") == "artifact_tool"
            and set(body.get("data_egress") or []) == {"artifact"}
        )
        if not valid:
            raise ArtifactReportingError("CSP artifact writer readiness 回應不符合契約")
        return True
    except (ArtifactReportingError, ValueError) as exc:
        _record_failure(str(exc))
        logger.error("artifact writer readiness failed: %s", exc)
        return False


async def _readiness_monitor() -> None:
    """Periodically revalidate credential/capability with one bounded probe."""
    while True:
        await asyncio.sleep(settings.STUDIO_ARTIFACT_READINESS_PROBE_SECONDS)
        try:
            await probe_readiness()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - monitor must self-heal
            message = f"artifact readiness monitor unexpected error:{type(exc).__name__}"
            _record_failure(message)
            logger.exception(message)


def start_readiness_monitor() -> asyncio.Task[None] | None:
    if not settings.STUDIO_ARTIFACT_REPORTING:
        return None
    return asyncio.create_task(
        _readiness_monitor(), name="studio-artifact-readiness-monitor"
    )


async def stop_readiness_monitor(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def report_job_created(
    *,
    bearer: str | None,
    job_id: str,
    artifact_type: str,
    status: str,
    requester_user_id: int,
    employee_id: str | None = None,
    collection_id: int | None = None,
    task_id: str | int | None = None,
    source_snapshot_id: str | int | None = None,
    trace_id: str | None = None,
) -> None:
    """Register one Studio job using the CSP owner/binding wire schema."""
    del bearer  # Browser credentials must never cross the service-only boundary.
    body: dict[str, Any] = {
        "job_id": job_id,
        "artifact_type": artifact_type,
        "status": status,
        "requester_user_id": requester_user_id,
    }
    if employee_id:
        body["employee_id"] = employee_id
    if collection_id is not None:
        body["collection_id"] = int(collection_id)
    parsed_task = _binding_id(task_id, field="task_id")
    parsed_snapshot = _binding_id(source_snapshot_id, field="source_snapshot_id")
    if parsed_task is not None:
        body["task_id"] = parsed_task
    if parsed_snapshot is not None:
        body["source_snapshot_id"] = parsed_snapshot
    if trace_id is not None:
        body["trace_id"] = trace_id
    await _send("POST", f"{settings.CSP_BASE_URL}/v1/artifact-jobs", json_body=body)


async def read_job_authority(job_id: str) -> dict[str, Any] | None:
    """Read CSP's committed job/artifact authority for crash convergence.

    A 404 means the queue item has not yet been registered.  Any other
    rejection remains a hard control-plane error; callers must not regenerate
    work when CSP may already own a committed artifact.
    """
    try:
        response = await _send(
            "GET",
            f"{settings.CSP_BASE_URL}/v1/artifact-jobs/{job_id}/authority",
            allowed_statuses=frozenset({404}),
        )
        if response.status_code == 404:
            return None
    except ArtifactReportingError:
        raise
    try:
        data = response.json()
    except ValueError as exc:
        raise ArtifactReportingError("artifact job authority returned non-JSON body") from exc
    if not isinstance(data, dict) or data.get("job_id") != job_id:
        raise ArtifactReportingError("artifact job authority response mismatch")
    return data


async def report_job_lease(
    *,
    job_id: str,
    attempt: int,
    lease_token: str,
) -> None:
    """Refresh CSP's fenced sink-admission lease for one Redis attempt."""
    await _send(
        "PUT",
        f"{settings.CSP_BASE_URL}/v1/artifact-jobs/{job_id}/lease",
        json_body={
            "attempt": attempt,
            "lease_token": lease_token,
            "lease_seconds": int(settings.STUDIO_JOB_LEASE_SECONDS),
        },
    )


async def report_job_patched(
    *,
    bearer: str | None,
    job_id: str,
    status: str,
    error: str | dict[str, Any] | None = None,
    progress: float | None = None,
    artifact_id: int | None = None,
) -> None:
    del bearer
    body: dict[str, Any] = {"status": status}
    if error is not None:
        body["error"] = error if isinstance(error, dict) else {"message": error}
    if progress is not None:
        body["progress"] = int(progress)
    if artifact_id is not None:
        body["artifact_id"] = int(artifact_id)
    await _send(
        "PATCH",
        f"{settings.CSP_BASE_URL}/v1/artifact-jobs/{job_id}",
        json_body=body,
    )


async def register_artifact(
    *,
    bearer: str | None,
    artifact_type: str,
    title: str | None,
    content: bytes,
    original_filename: str,
    media_type: str,
    job_id: str | None = None,
    task_id: str | int | None = None,
    source_snapshot_id: str | int | None = None,
    content_hash: str,
    classification_level: str | None = None,
    metadata: dict[str, Any] | None = None,
    attempt: int | None = None,
    lease_token: str | None = None,
) -> dict[str, Any]:
    del bearer
    body: dict[str, Any] = {
        "artifact_type": artifact_type,
        "title": title or f"{artifact_type}:{job_id or 'artifact'}",
        "content_sha256": content_hash,
        "content_size": len(content),
        "media_type": media_type,
        "original_filename": original_filename,
    }
    if job_id is not None:
        body["job_id"] = job_id
    parsed_task = _binding_id(task_id, field="task_id")
    parsed_snapshot = _binding_id(source_snapshot_id, field="source_snapshot_id")
    if parsed_task is not None:
        body["task_id"] = parsed_task
    if parsed_snapshot is not None:
        body["source_snapshot_id"] = parsed_snapshot
    if content_hash is not None:
        body["content_hash"] = content_hash
    if classification_level is not None:
        body["classification_level"] = classification_level
    if metadata:
        body["metadata"] = metadata
    response = await _send(
        "POST",
        f"{settings.CSP_BASE_URL}/v1/artifacts/upload",
        data={"metadata_json": json.dumps(body, ensure_ascii=False)},
        files={"file": (original_filename, content, media_type)},
        extra_headers=(
            {
                "X-Studio-Attempt": str(attempt),
                "X-Studio-Lease-Token": lease_token,
            }
            if attempt is not None and lease_token
            else None
        ),
    )
    try:
        data = response.json()
    except ValueError as exc:
        message = "artifact register returned non-JSON body"
        _record_failure(message)
        raise ArtifactReportingError(message) from exc
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("artifact_id"), int)
        or not isinstance(data.get("version_id"), int)
        or not isinstance(data.get("download_url"), str)
    ):
        message = "artifact upload response missing integer ids/download_url"
        _record_failure(message)
        raise ArtifactReportingError(message)
    return data


__all__ = [
    "ArtifactReportingError",
    "mark_reporting_failure",
    "probe_readiness",
    "register_artifact",
    "read_job_authority",
    "report_job_created",
    "report_job_lease",
    "report_job_patched",
    "reporting_status",
    "reset_reporting_status",
    "start_readiness_monitor",
    "stop_readiness_monitor",
]
