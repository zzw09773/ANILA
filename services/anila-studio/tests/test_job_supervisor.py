"""Restart/registry contract tests for the durable Studio supervisor."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.job_lifecycle import JobReportContext
from app.services.job_store import PersistedJob
from app.services.job_supervisor import _hydrate_updater, _run_registered_pipeline
from app.services.runtime_context import StudioRuntimeContext


def _cases() -> list[tuple[str, dict[str, Any]]]:
    common = {
        "collection_id": 5,
        "task_id": "101",
        "source_snapshot_id": "202",
        "trace_id": "trace-1",
    }
    return [
        ("slides", {**common, "preset": "training"}),
        ("report", {**common, "preset": "key_summary"}),
        ("mindmap", {**common, "preset": "concept_tree"}),
        ("infographic", {**common, "preset": "stats_brief"}),
        ("datatable", {**common, "preset": "key_figures"}),
    ]


def _job(artifact_type: str, request_spec: dict[str, Any]) -> PersistedJob:
    return PersistedJob(
        job_id=f"restart-{artifact_type}",
        artifact_type=artifact_type,
        owner_user_id=7,
        requester="EMP0007",
        collection_id=5,
        task_id="101",
        source_snapshot_id="202",
        trace_id="trace-1",
        state="running",
        status_view={"job_id": f"restart-{artifact_type}", "state": "running"},
        request_spec=request_spec,
        attempt_count=2,
        restart_count=1,
        lease_token="lease-restart",
        lease_owner="worker-after-restart",
        lease_expires_at_epoch=4_102_444_800.0,
    )


@pytest.mark.parametrize("artifact_type,request_spec", _cases())
async def test_restart_registry_rehydrates_and_runs_all_five(
    artifact_type: str,
    request_spec: dict[str, Any],
    monkeypatch,
) -> None:
    """A process with empty module registries rebuilds each Pydantic runner."""
    job = _job(artifact_type, request_spec)
    ctx = JobReportContext(
        artifact_type=artifact_type,
        owner_user_id=7,
        bearer="",
        collection_id=5,
        requester="EMP0007",
        task_id="101",
        source_snapshot_id="202",
        trace_id="trace-1",
        request_spec=request_spec,
        _lease_token="lease-restart",
        _synchronous_finalize=True,
    )
    updater, module = await _hydrate_updater(job, ctx)
    calls: list[tuple[Any, str]] = []

    if artifact_type == "slides":
        from app.api import studio

        async def fake(**kwargs: Any) -> None:
            calls.append((kwargs["payload"], kwargs["bearer"]))

        monkeypatch.setattr(studio, "_run_pipeline", fake)
    elif artifact_type == "report":
        from app.services import report_runner

        async def fake(**kwargs: Any) -> None:
            calls.append((kwargs["request"], kwargs["bearer"]))

        monkeypatch.setattr(report_runner, "run_report_pipeline", fake)
    elif artifact_type == "mindmap":
        from app.api import mindmaps

        async def fake(**kwargs: Any) -> None:
            calls.append((kwargs["payload"], kwargs["bearer"]))

        monkeypatch.setattr(mindmaps, "_run_pipeline", fake)
    elif artifact_type == "infographic":
        from app.api import infographics

        async def fake(**kwargs: Any) -> None:
            calls.append((kwargs["payload"], kwargs["bearer"]))

        monkeypatch.setattr(infographics, "_run_pipeline", fake)
    else:
        from app.api import datatables

        async def fake(**kwargs: Any) -> None:
            calls.append((kwargs["payload"], kwargs["bearer"]))

        monkeypatch.setattr(datatables, "_run_pipeline", fake)

    try:
        await _run_registered_pipeline(job, updater)
        assert len(calls) == 1
        payload, bearer = calls[0]
        assert payload.collection_id == 5
        assert str(payload.task_id) == "101"
        assert str(payload.source_snapshot_id) == "202"
        assert bearer == ""
        assert module._jobs[job.job_id].user_id == 7  # noqa: SLF001
    finally:
        module._jobs.pop(job.job_id, None)  # noqa: SLF001


def test_runtime_context_uses_separate_service_token_without_browser_bearer(
    monkeypatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "STUDIO_RUNTIME_SERVICE_TOKEN", "csk-runtime")
    monkeypatch.setattr(settings, "STUDIO_ARTIFACT_SERVICE_TOKEN", "csk-artifact")
    headers = StudioRuntimeContext.from_job(_job("slides", _cases()[0][1])).headers()
    assert headers["X-CSP-Service-Token"] == "csk-runtime"
    assert headers["X-ANILA-Task-Id"] == "101"
    assert headers["X-ANILA-User-Id"] == "EMP0007"
    assert headers["X-Studio-Snapshot-Id"] == "202"
    assert headers["X-Studio-Attempt"] == "2"
    assert headers["X-Studio-Lease-Token"] == "lease-restart"
    assert "Authorization" not in headers
    assert "Bearer" not in repr(headers)


@pytest.mark.asyncio
async def test_restart_after_csp_commit_converges_without_regeneration_or_lease(
    monkeypatch,
) -> None:
    from app.services import job_lifecycle, job_reporting, job_supervisor

    job = _job("report", _cases()[1][1])
    completed: list[PersistedJob] = []
    pipeline_calls = 0
    lease_calls = 0

    class _Store:
        async def complete(self, recovered, token):
            assert token == job.lease_token
            completed.append(recovered)

    async def fake_created(**_kwargs):
        return None

    async def fake_authority(_job_id):
        return {
            "job_id": job.job_id,
            "status": "completed",
            "artifact_id": 44,
            "current_version": 3,
            "classification_level": "機密",
            "content_hash": "a" * 64,
            "download_url": "/api/artifacts/44/versions/9/download",
        }

    async def fake_lease(**_kwargs):
        nonlocal lease_calls
        lease_calls += 1

    async def fail_pipeline(*_args, **_kwargs):
        nonlocal pipeline_calls
        pipeline_calls += 1
        raise AssertionError("committed artifact must not regenerate")

    monkeypatch.setattr(job_supervisor, "get_job_store", lambda: _Store())
    monkeypatch.setattr(job_reporting, "report_job_created", fake_created)
    monkeypatch.setattr(job_reporting, "read_job_authority", fake_authority)
    monkeypatch.setattr(job_reporting, "report_job_lease", fake_lease)
    monkeypatch.setattr(job_supervisor, "_run_registered_pipeline", fail_pipeline)
    monkeypatch.setattr(job_lifecycle, "_start_heartbeat", lambda *_args: None)

    await job_supervisor.JobSupervisor()._execute(job)  # noqa: SLF001

    assert pipeline_calls == 0
    assert lease_calls == 0
    assert len(completed) == 1
    assert completed[0].state == "done"
    assert completed[0].artifact_id == 44
    assert completed[0].download_url.endswith("/download")
    assert completed[0].result_metadata["recovered_after_commit"] is True


@pytest.mark.asyncio
async def test_heartbeat_refreshes_redis_and_csp_with_same_fence(monkeypatch) -> None:
    from app.services import job_lifecycle, job_reporting

    redis_calls: list[tuple[str, str]] = []
    csp_calls: list[tuple[int, str]] = []
    ctx = JobReportContext(
        artifact_type="report",
        owner_user_id=7,
        bearer="",
        _lease_token="lease-token-123456789",
        _attempt_count=3,
    )

    class _Store:
        async def heartbeat(self, job_id, token, *, lease_seconds):
            redis_calls.append((job_id, token))

    async def fake_sleep(_seconds):
        return None

    async def fake_lease(*, job_id, attempt, lease_token):
        csp_calls.append((attempt, lease_token))
        if len(csp_calls) == 2:
            ctx._durable_terminal = True

    monkeypatch.setattr(job_lifecycle, "get_job_store", lambda: _Store())
    monkeypatch.setattr(job_lifecycle.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(job_reporting, "report_job_lease", fake_lease)

    await job_lifecycle._heartbeat_loop("job-heartbeat", ctx)  # noqa: SLF001

    assert redis_calls == [
        ("job-heartbeat", "lease-token-123456789"),
        ("job-heartbeat", "lease-token-123456789"),
    ]
    assert csp_calls == [
        (3, "lease-token-123456789"),
        (3, "lease-token-123456789"),
    ]


@pytest.mark.asyncio
async def test_max_attempt_preserves_cancelled_terminal_target(monkeypatch) -> None:
    from app.config import settings
    from app.services import job_lifecycle, job_reporting, job_supervisor

    job = replace(
        _job("report", _cases()[1][1]),
        attempt_count=2,
        max_attempts=2,
        checkpoint={
            "terminal_status": "cancelled",
            "local_terminal_state": "cancelled",
        },
    )
    completed: list[PersistedJob] = []
    reported: list[str] = []

    class _Store:
        current = job

        async def get(self, _job_id):
            return self.current

        async def update_claimed(self, pending, token):
            assert token == job.lease_token
            self.current = pending

        async def complete(self, terminal, token):
            assert token == job.lease_token
            completed.append(terminal)

        async def defer_terminal_report(self, *_args, **_kwargs):
            raise AssertionError("successful terminal report must not be deferred")

    class _Updater:
        async def set(self, **_changes):
            return None

    store = _Store()

    async def fake_hydrate(_job, _ctx):
        return _Updater(), SimpleNamespace(artifact_info=lambda _record: None)

    async def fake_created(**_kwargs):
        return None

    async def fake_authority(_job_id):
        return {"job_id": job.job_id, "status": "running", "artifact_id": None}

    async def fake_lease(**_kwargs):
        return None

    async def fail_pipeline(*_args, **_kwargs):
        raise RuntimeError("cancel acknowledged after final attempt")

    async def fake_patch(*, status, **_kwargs):
        reported.append(status)

    async def fake_stop(_ctx):
        return None

    async def fake_drop(*_args):
        return None

    monkeypatch.setattr(settings, "STUDIO_RUNTIME_SERVICE_TOKEN", "csk-runtime")
    monkeypatch.setattr(job_supervisor, "get_job_store", lambda: store)
    monkeypatch.setattr(job_supervisor, "_hydrate_updater", fake_hydrate)
    monkeypatch.setattr(job_supervisor, "_run_registered_pipeline", fail_pipeline)
    monkeypatch.setattr(job_supervisor, "_drop_live_projection", fake_drop)
    monkeypatch.setattr(job_reporting, "report_job_created", fake_created)
    monkeypatch.setattr(job_reporting, "read_job_authority", fake_authority)
    monkeypatch.setattr(job_reporting, "report_job_lease", fake_lease)
    monkeypatch.setattr(job_reporting, "report_job_patched", fake_patch)
    monkeypatch.setattr(job_lifecycle, "_start_heartbeat", lambda *_args: None)
    monkeypatch.setattr(job_lifecycle, "_stop_heartbeat", fake_stop)

    await job_supervisor.JobSupervisor()._execute(job)  # noqa: SLF001

    assert reported == ["cancelled"]
    assert len(completed) == 1
    assert completed[0].state == "cancelled"
    assert completed[0].stage == "cancelled"
    assert completed[0].status_view["state"] == "cancelled"
    assert completed[0].checkpoint == {}
