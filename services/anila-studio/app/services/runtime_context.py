"""Task-scoped runtime delegation carried only in process memory."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

from app.config import settings
from app.services.job_store import PersistedJob


@dataclass(frozen=True)
class StudioRuntimeContext:
    job_id: str
    artifact_type: str
    requester_user_id: int
    requester_employee_id: str | None
    collection_id: int
    task_id: int
    source_snapshot_id: int
    trace_id: str | None
    attempt: int
    lease_token: str

    @classmethod
    def from_job(cls, job: PersistedJob) -> "StudioRuntimeContext":
        try:
            collection_id = int(job.collection_id or 0)
            task_id = int(job.task_id or 0)
            snapshot_id = int(job.source_snapshot_id or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("durable job collection/task/snapshot 綁定格式錯誤") from exc
        if min(collection_id, task_id, snapshot_id) < 1:
            raise ValueError("durable job 必須綁正整數 collection/task/snapshot")
        if job.attempt_count < 1 or not job.lease_token:
            raise ValueError("durable job 必須具有 fenced attempt/lease")
        if not (job.trace_id or "").strip():
            raise ValueError("durable job 必須綁定 authoritative Full Trace")
        return cls(
            job_id=job.job_id,
            artifact_type=job.artifact_type,
            requester_user_id=job.owner_user_id,
            requester_employee_id=job.requester,
            collection_id=collection_id,
            task_id=task_id,
            source_snapshot_id=snapshot_id,
            trace_id=job.trace_id,
            attempt=job.attempt_count,
            lease_token=(job.lease_token or ""),
        )

    def headers(self) -> dict[str, str]:
        token = settings.STUDIO_RUNTIME_SERVICE_TOKEN.strip()
        if not token or token == settings.STUDIO_ARTIFACT_SERVICE_TOKEN.strip():
            raise RuntimeError("Studio runtime token 缺失或與 artifact writer 共用")
        headers = {
            "X-CSP-Service-Token": token,
            "X-Studio-Job-Id": self.job_id,
            "X-Studio-Artifact-Type": self.artifact_type,
            "X-Studio-Requester-User-Id": str(self.requester_user_id),
            "X-Studio-Collection-Id": str(self.collection_id),
            "X-Studio-Task-Id": str(self.task_id),
            "X-ANILA-Task-Id": str(self.task_id),
            "X-Studio-Snapshot-Id": str(self.source_snapshot_id),
            "X-Studio-Attempt": str(self.attempt),
            "X-Studio-Lease-Token": self.lease_token,
        }
        if self.requester_employee_id:
            headers["X-Studio-Requester-Employee-Id"] = self.requester_employee_id
            headers["X-ANILA-User-Id"] = self.requester_employee_id
        if self.trace_id:
            headers["X-ANILA-Trace-Id"] = self.trace_id
        return headers


_current: ContextVar[StudioRuntimeContext | None] = ContextVar(
    "studio_runtime_context",
    default=None,
)


def current_runtime_context() -> StudioRuntimeContext | None:
    return _current.get()


@contextmanager
def use_runtime_context(context: StudioRuntimeContext) -> Iterator[None]:
    token = _current.set(context)
    try:
        yield
    finally:
        _current.reset(token)


__all__ = [
    "StudioRuntimeContext",
    "current_runtime_context",
    "use_runtime_context",
]
