"""Tests for the Slice 8b durable job store + CSP reporting + trace spans.

Covers:
  * JobStore round-trip + restart survival (fakeredis, mirroring the
    revocation-cache test's shared FakeServer pattern).
  * All five pipelines registering their job through the store on create.
  * CSP reporting calls (POST /v1/artifact-jobs, PATCH, POST /v1/artifacts)
    with correct payloads incl. task/snapshot/requester passthrough (respx).
  * CSP-down/4xx failures degrade readiness and fail completion closed.
  * Trace spans emitted only when a trace_id is present.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fakeredis.aioredis
import httpx
import pytest
import respx
from pydantic import BaseModel

from app.config import settings
from app.services import job_lifecycle, job_reporting
from app.services.job_lifecycle import ArtifactInfo, JobReportContext
from app.services.job_store import JobStore, PersistedJob, UnsafeJobPayload
from app.services.studio_trace import StudioTraceEmitter


_CSP = settings.CSP_BASE_URL
_CONTRACT = json.loads(
    (Path(__file__).resolve().parents[3] / "contracts/fixtures/artifact-control-plane-v1.json")
    .read_text(encoding="utf-8")
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_redis_server() -> fakeredis.aioredis.FakeServer:
    return fakeredis.aioredis.FakeServer()


@pytest.fixture
def fake_redis_factory(fake_redis_server, monkeypatch):
    """Patch ``job_store._from_url`` to hand out FakeRedis bound to a shared
    server (so a "restarted" store sees the same keyspace)."""

    def _factory(url: str, **kwargs: Any):
        return fakeredis.aioredis.FakeRedis(
            server=fake_redis_server,
            decode_responses=kwargs.get("decode_responses", True),
        )

    monkeypatch.setattr("redis.asyncio.from_url", _factory)
    from app.services import job_store as js_mod

    monkeypatch.setattr(js_mod, "_from_url", _factory, raising=False)
    return _factory


@pytest.fixture
async def started_store(fake_redis_factory) -> JobStore:
    store = JobStore()
    await store.start()
    try:
        yield store
    finally:
        await store.stop()


@pytest.fixture
def respx_mock():
    with respx.mock(assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def reporting_on(monkeypatch):
    monkeypatch.setattr(settings, "STUDIO_ARTIFACT_REPORTING", True)
    monkeypatch.setattr(settings, "STUDIO_DURABLE_SUPERVISOR", False)
    monkeypatch.setattr(
        settings, "STUDIO_ARTIFACT_SERVICE_TOKEN", "csk-studio-artifact-test"
    )
    job_reporting.reset_reporting_status()

    class _LifecycleStore:
        def __init__(self) -> None:
            self.jobs: dict[str, PersistedJob] = {}

        async def enqueue(self, job: PersistedJob) -> None:
            self.jobs[job.job_id] = job

        async def put_quietly(self, job: PersistedJob) -> None:
            self.jobs[job.job_id] = job

        async def get(self, job_id: str) -> PersistedJob | None:
            return self.jobs.get(job_id)

        async def update_claimed(
            self, job: PersistedJob, _lease_token: str | None
        ) -> None:
            self.jobs[job.job_id] = job

        async def complete(
            self, job: PersistedJob, _lease_token: str | None
        ) -> None:
            self.jobs[job.job_id] = job

    store = _LifecycleStore()
    monkeypatch.setattr(job_lifecycle, "get_job_store", lambda: store)
    return store


def _persisted(job_id: str = "j1", **over: Any) -> PersistedJob:
    base = dict(
        job_id=job_id,
        artifact_type="slides",
        owner_user_id=7,
        state="done",
        status_view={"job_id": job_id, "state": "done"},
        collection_id=5,
        task_id="task_1",
        source_snapshot_id="snap_2",
        trace_id="trace_9",
        requester="EMP42",
    )
    base.update(over)
    return PersistedJob(**base)


def test_durable_envelope_hmac_rejects_authority_or_request_tampering():
    original = _persisted(
        request_spec={"title": "approved"},
        task_id="21",
        source_snapshot_id="31",
    )
    payload = json.loads(original.to_json())
    payload["request_spec"] = {"title": "attacker-controlled"}
    payload["task_id"] = "999"

    with pytest.raises(UnsafeJobPayload, match="integrity check failed"):
        PersistedJob.from_json(json.dumps(payload))


# ---------------------------------------------------------------------------
# JobStore round-trip + restart survival
# ---------------------------------------------------------------------------


async def test_job_store_round_trip(started_store):
    job = _persisted()
    await started_store.put(job)
    got = await started_store.get("j1")
    assert got == job  # frozen dataclass equality
    assert await started_store.get("does-not-exist") is None


async def test_job_store_survives_restart(fake_redis_factory):
    """A fresh JobStore instance against the same fake redis (simulating a
    studio restart) can still answer a pre-restart job's status query."""
    s1 = JobStore()
    await s1.start()
    await s1.put(_persisted(job_id="jr", status_view={"job_id": "jr", "state": "done"}))
    await s1.stop()  # process "dies"

    s2 = JobStore()  # process "restarts": brand-new instance
    await s2.start()
    try:
        got = await s2.get("jr")
        assert got is not None
        assert got.job_id == "jr"
        assert got.status_view == {"job_id": "jr", "state": "done"}
    finally:
        await s2.stop()


async def test_read_status_read_through(started_store, monkeypatch):
    """job_lifecycle.read_status returns the stored status_view for the owner
    and None for a cross-user / missing job."""
    monkeypatch.setattr(job_lifecycle, "get_job_store", lambda: started_store)
    await started_store.put(_persisted(job_id="jx", owner_user_id=7))
    assert await job_lifecycle.read_status("jx", 7) == {"job_id": "jx", "state": "done"}
    assert await job_lifecycle.read_status("jx", 99) is None  # cross-user
    assert await job_lifecycle.read_status("nope", 7) is None


# ---------------------------------------------------------------------------
# All five pipelines register through the store on create
# ---------------------------------------------------------------------------


def _pipeline_params():
    from app.schemas.infographic import InfographicPreset
    from app.schemas.mindmap import MindmapPreset
    from app.schemas.report import ReportPreset
    from app.services import (
        datatable_job_service,
        infographic_job_service,
        mindmap_job_service,
        report_job_service,
        studio_job_service,
    )

    return [
        ("slides", studio_job_service, {}),
        ("report", report_job_service, {"preset": ReportPreset.KEY_SUMMARY}),
        ("mindmap", mindmap_job_service, {"preset": MindmapPreset.CONCEPT_TREE}),
        (
            "infographic",
            infographic_job_service,
            {"preset": InfographicPreset.STATS_BRIEF},
        ),
        ("datatable", datatable_job_service, {}),
    ]


@pytest.mark.parametrize("artifact_type,module,extra", _pipeline_params())
async def test_pipeline_registers_job_in_store(
    artifact_type, module, extra, fake_redis_factory, monkeypatch
):
    """create_job persists the freshly-created job through the JobStore for
    every one of the five pipelines."""
    # Reporting off: this test is about the durable store, not CSP.
    monkeypatch.setattr(settings, "STUDIO_ARTIFACT_REPORTING", False)
    monkeypatch.setattr(settings, "STUDIO_DURABLE_SUPERVISOR", True)
    class _RecordingStore:
        def __init__(self) -> None:
            self.jobs: dict[str, PersistedJob] = {}

        async def enqueue(self, job: PersistedJob) -> None:
            self.jobs[job.job_id] = job

        async def get(self, job_id: str) -> PersistedJob | None:
            return self.jobs.get(job_id)

    store = _RecordingStore()
    monkeypatch.setattr(job_lifecycle, "get_job_store", lambda: store)
    try:
        ctx = JobReportContext(
            artifact_type=artifact_type,
            owner_user_id=13,
            bearer="B",
            collection_id=5,
            describe=module.artifact_info,
        )

        async def _noop(_updater: Any) -> None:
            return None

        record = await module.create_job(
            user_id=13, collection_id=5, runner=_noop, report_ctx=ctx, **extra
        )
        persisted = await store.get(record.job_id)
        assert persisted is not None
        assert persisted.artifact_type == artifact_type
        assert persisted.owner_user_id == 13
        assert persisted.collection_id == 5
        assert persisted.status_view["job_id"] == record.job_id
    finally:
        module._jobs.pop(record.job_id, None)


@pytest.mark.parametrize("artifact_type,module,extra", _pipeline_params())
async def test_pipeline_status_accepts_integer_csp_artifact_id(
    artifact_type, module, extra, monkeypatch
):
    """Exercise each real updater/to_status path with CSP's integer ID."""
    monkeypatch.setattr(settings, "STUDIO_ARTIFACT_REPORTING", False)
    monkeypatch.setattr(settings, "STUDIO_DURABLE_SUPERVISOR", False)

    async def _write_control_plane_result(updater: Any) -> None:
        await updater.set(
            state="done", artifact_id=9, classification_level="無機密"
        )

    record = await module.create_job(
        user_id=13,
        collection_id=5,
        runner=_write_control_plane_result,
        report_ctx=None,
        **extra,
    )
    live = None
    for _ in range(50):
        live = module.get_job(record.job_id)
        if live is not None and live.artifact_id == 9:
            break
        await asyncio.sleep(0.01)
    assert live is not None
    status = live.to_status()
    assert status.artifact_id == 9
    assert status.classification_level == "無機密"


# ---------------------------------------------------------------------------
# CSP reporting payloads (respx)
# ---------------------------------------------------------------------------


class _View(BaseModel):
    job_id: str
    state: str
    title: str | None = None
    artifact_id: int | None = None
    classification_level: str | None = None


@dataclass
class _FakeRec:
    job_id: str
    user_id: int
    collection_id: int
    state: str
    step: str | None = None
    title: str | None = None
    error: str | None = None
    artifact_id: int | None = None
    classification_level: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_status(self) -> _View:
        return _View(
            job_id=self.job_id,
            state=self.state,
            title=self.title,
            artifact_id=self.artifact_id,
            classification_level=self.classification_level,
        )


@dataclass
class _FakeUpdater:
    calls: list = field(default_factory=list)

    async def set(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


_PPTX = b"PK\x03\x04PPTX-PRIMARY-BYTES"


def _slides_describe(rec: Any) -> ArtifactInfo:
    return ArtifactInfo(
        artifact_type="slides",
        title=rec.title,
        storage_ref=f"/api/studio/slides/jobs/{rec.job_id}/pptx",
        primary_bytes=_PPTX,
        original_filename=f"{rec.job_id}.pptx",
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        result_metadata={"slide_count": 3},
    )


def _make_ctx(*, trace_id: str | None = None) -> JobReportContext:
    return JobReportContext(
        artifact_type="slides",
        owner_user_id=7,
        bearer="BEARER-XYZ",
        collection_id=5,
        requester="EMP42",
        task_id="101",
        source_snapshot_id="202",
        trace_id=trace_id,
        describe=_slides_describe,
        emitter=StudioTraceEmitter(trace_id=trace_id, endpoint=_CSP, bearer="BEARER-XYZ"),
    )


async def test_csp_reporting_full_cycle(reporting_on, respx_mock):
    created = respx_mock.post(f"{_CSP}/v1/artifact-jobs").mock(
        return_value=httpx.Response(201, json={"job_id": "j1"})
    )
    registered = respx_mock.post(f"{_CSP}/v1/artifacts/upload").mock(
        return_value=httpx.Response(
            201,
            json={
                "artifact_id": 9,
                "version_id": 1,
                "classification_level": "機密",
                "download_url": "/api/artifacts/9/versions/1/download",
            },
        )
    )
    patched = respx_mock.patch(f"{_CSP}/v1/artifact-jobs/j1").mock(
        return_value=httpx.Response(200, json={})
    )

    ctx = _make_ctx()
    await job_lifecycle.on_create(
        _FakeRec(job_id="j1", user_id=7, collection_id=5, state="pending", step="queued"),
        ctx,
    )
    updater = _FakeUpdater()
    await job_lifecycle.on_transition(
        _FakeRec(job_id="j1", user_id=7, collection_id=5, state="done", step="done", title="Deck"),
        ctx,
        updater,
    )
    await job_lifecycle.drain()

    # POST /v1/artifact-jobs — inheritance passthrough + requester.
    assert created.called
    create_body = json.loads(created.calls.last.request.content)
    assert create_body["job_id"] == "j1"
    assert create_body["artifact_type"] == "slides"
    assert create_body["task_id"] == 101
    assert create_body["source_snapshot_id"] == 202
    assert create_body["requester_user_id"] == 7
    assert create_body["employee_id"] == "EMP42"
    assert "Authorization" not in created.calls.last.request.headers
    assert (
        created.calls.last.request.headers["X-CSP-Service-Token"]
        == "csk-studio-artifact-test"
    )

    # POST /v1/artifacts/upload — bytes + verified claims, never storage_ref.
    assert registered.called
    reg_body = registered.calls.last.request.content
    assert b'"artifact_type": "slides"' in reg_body
    assert b'"task_id": 101' in reg_body
    assert b'"source_snapshot_id": 202' in reg_body
    assert hashlib.sha256(_PPTX).hexdigest().encode() in reg_body
    assert _PPTX in reg_body
    assert b"storage_ref" not in reg_body

    # PATCH /v1/artifact-jobs/{id} — terminal status mapped + artifact_id.
    assert patched.called
    patch_body = json.loads(patched.calls.last.request.content)
    assert patch_body["status"] == "completed"
    assert patch_body["artifact_id"] == 9

    # Registered artifact metadata written back onto the record.
    assert updater.calls
    assert updater.calls[-1]["artifact_id"] == 9
    assert updater.calls[-1]["classification_level"] == "機密"


async def test_terminal_registration_waits_for_job_create(
    reporting_on, monkeypatch
):
    """A fast terminal transition cannot register before CSP has its job row."""
    create_started = asyncio.Event()
    release_create = asyncio.Event()
    order: list[str] = []

    async def _blocked_create(**_kwargs: Any) -> None:
        order.append("create-started")
        create_started.set()
        await release_create.wait()
        order.append("create-completed")

    async def _register(**_kwargs: Any) -> dict[str, Any]:
        order.append("artifact-registered")
        return {"artifact_id": 9, "classification_level": "無機密"}

    async def _patch(**_kwargs: Any) -> None:
        order.append("job-patched")

    monkeypatch.setattr(job_reporting, "report_job_created", _blocked_create)
    monkeypatch.setattr(job_reporting, "register_artifact", _register)
    monkeypatch.setattr(job_reporting, "report_job_patched", _patch)

    ctx = _make_ctx()
    await job_lifecycle.on_create(
        _FakeRec(job_id="ordered", user_id=7, collection_id=5, state="pending"),
        ctx,
    )
    await create_started.wait()
    await job_lifecycle.on_transition(
        _FakeRec(
            job_id="ordered",
            user_id=7,
            collection_id=5,
            state="done",
            title="Ordered",
        ),
        ctx,
        _FakeUpdater(),
    )
    await asyncio.sleep(0.02)
    assert "artifact-registered" not in order

    release_create.set()
    await job_lifecycle.drain()
    assert order == [
        "create-started",
        "create-completed",
        "artifact-registered",
        "job-patched",
    ]
    assert ctx._create_report_task is None


async def test_shared_wire_fixture_is_emitted_exactly(reporting_on, respx_mock):
    """Studio emits the fixture CSP validates and executes in its own suite."""
    created = respx_mock.post(f"{_CSP}/v1/artifact-jobs").mock(
        return_value=httpx.Response(201, json={"job_id": "contract-job-1"})
    )
    registered = respx_mock.post(f"{_CSP}/v1/artifacts/upload").mock(
        return_value=httpx.Response(
            201,
            json={
                "artifact_id": 9,
                "version_id": 3,
                "classification_level": "無機密",
                "download_url": "/api/artifacts/9/versions/3/download",
            },
        )
    )
    patched = respx_mock.patch(
        f"{_CSP}/v1/artifact-jobs/contract-job-1"
    ).mock(return_value=httpx.Response(200, json={}))

    await job_reporting.report_job_created(
        bearer="browser-jwt-must-not-forward",
        job_id="contract-job-1",
        artifact_type="slides",
        status="queued",
        requester_user_id=7,
        employee_id="EMP42",
        task_id="101",
        trace_id="trace-contract-1",
    )
    await job_reporting.register_artifact(
        bearer="browser-jwt-must-not-forward",
        artifact_type="slides",
        title="Deck",
        content=_PPTX,
        original_filename="contract-job-1.pptx",
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        job_id="contract-job-1",
        task_id="101",
        source_snapshot_id="202",
        content_hash=hashlib.sha256(_PPTX).hexdigest(),
        metadata={"slide_count": 3},
        attempt=1,
        lease_token="lease-token-123456789",
    )
    await job_reporting.report_job_patched(
        bearer="browser-jwt-must-not-forward",
        job_id="contract-job-1",
        status="completed",
        artifact_id=9,
    )

    assert json.loads(created.calls.last.request.content) == _CONTRACT["job_created"]
    registered_body = registered.calls.last.request.content
    for key, value in _CONTRACT["artifact_registered"].items():
        if key == "metadata":
            assert b'"slide_count": 3' in registered_body
        elif isinstance(value, int):
            assert f'"{key}": {value}'.encode() in registered_body
        else:
            assert str(value).encode() in registered_body
    assert _PPTX in registered_body
    assert json.loads(patched.calls.last.request.content) == _CONTRACT["job_completed"]
    for route in (created, registered, patched):
        headers = route.calls.last.request.headers
        assert "Authorization" not in headers
        assert headers["X-CSP-Service-Token"] == "csk-studio-artifact-test"


async def test_csp_reporting_failed_state_patch(reporting_on, respx_mock):
    """A failed job PATCHes status=failed with the error and skips register."""
    respx_mock.post(f"{_CSP}/v1/artifact-jobs").mock(return_value=httpx.Response(201))
    registered = respx_mock.post(f"{_CSP}/v1/artifacts/upload").mock(
        return_value=httpx.Response(201, json={"artifact_id": "x"})
    )
    patched = respx_mock.patch(f"{_CSP}/v1/artifact-jobs/jf").mock(
        return_value=httpx.Response(200)
    )
    ctx = _make_ctx()
    await job_lifecycle.on_transition(
        _FakeRec(job_id="jf", user_id=7, collection_id=5, state="failed", error="boom"),
        ctx,
        _FakeUpdater(),
    )
    await job_lifecycle.drain()
    assert not registered.called  # no artifact on failure
    body = json.loads(patched.calls.last.request.content)
    assert body["status"] == "failed"
    assert body["error"] == {"message": "boom"}


async def test_csp_down_marks_readiness_and_completion_failed(reporting_on, respx_mock):
    """A control-plane outage cannot leave a generated artifact false-green."""
    respx_mock.post(f"{_CSP}/v1/artifact-jobs").mock(side_effect=httpx.ConnectError("down"))
    respx_mock.post(f"{_CSP}/v1/artifacts/upload").mock(return_value=httpx.Response(503))
    respx_mock.patch(url__regex=rf"{_CSP}/v1/artifact-jobs/.*").mock(
        side_effect=httpx.ConnectError("down")
    )
    ctx = _make_ctx()
    updater = _FakeUpdater()
    # Generation remains detached, but failure must be observable/fail-closed.
    await job_lifecycle.on_create(
        _FakeRec(job_id="jd", user_id=7, collection_id=5, state="pending"), ctx
    )
    await job_lifecycle.on_transition(
        _FakeRec(job_id="jd", user_id=7, collection_id=5, state="done", title="D"),
        ctx,
        updater,
    )
    await job_lifecycle.drain()
    assert any(call.get("state") == "failed" for call in updater.calls)
    assert job_reporting.reporting_status()["ready"] is False
    assert job_reporting.reporting_status()["last_error"]


async def test_artifact_4xx_stays_unready_after_compensating_failed_patch(
    reporting_on, respx_mock
):
    respx_mock.post(f"{_CSP}/v1/artifacts/upload").mock(
        return_value=httpx.Response(422, json={"detail": "binding rejected"})
    )
    patched = respx_mock.patch(f"{_CSP}/v1/artifact-jobs/j422").mock(
        return_value=httpx.Response(200, json={})
    )
    updater = _FakeUpdater()
    await job_lifecycle.on_transition(
        _FakeRec(
            job_id="j422",
            user_id=7,
            collection_id=5,
            state="done",
            title="Rejected",
        ),
        _make_ctx(),
        updater,
    )
    await job_lifecycle.drain()

    assert patched.called
    assert json.loads(patched.calls.last.request.content)["status"] == "failed"
    assert any(call.get("state") == "failed" for call in updater.calls)
    status = job_reporting.reporting_status()
    assert status["ready"] is False
    assert "產物控制面註冊失敗" in status["last_error"]


async def test_reporting_readiness_recovers_after_transient_outage(
    reporting_on, respx_mock
):
    probe = respx_mock.get(f"{_CSP}/v1/artifact-writer/ready").mock(
        side_effect=httpx.ConnectError("temporary")
    )
    assert await job_reporting.probe_readiness() is False
    assert job_reporting.reporting_status()["ready"] is False

    probe.mock(return_value=httpx.Response(
        200,
        json={
            "ready": True,
            "service_id": 1,
            "service_slug": "anila-studio-artifact",
            "service_type": "artifact_tool",
            "data_egress": ["artifact"],
        },
    ))
    assert await job_reporting.probe_readiness() is True
    assert job_reporting.reporting_status()["ready"] is True


async def test_background_reprobe_recovers_startup_degraded_state(
    reporting_on, respx_mock, monkeypatch
):
    route = respx_mock.get(f"{_CSP}/v1/artifact-writer/ready").mock(
        side_effect=httpx.ConnectError("startup outage")
    )
    assert await job_reporting.probe_readiness() is False

    route.mock(
        return_value=httpx.Response(
            200,
            json={
                "ready": True,
                "service_id": 1,
                "service_slug": "anila-studio-artifact",
                "service_type": "artifact_tool",
                "data_egress": ["artifact"],
            },
        )
    )
    monkeypatch.setattr(settings, "STUDIO_ARTIFACT_READINESS_PROBE_SECONDS", 0.01)
    monitor = job_reporting.start_readiness_monitor()
    try:
        for _ in range(50):
            if job_reporting.reporting_status()["ready"]:
                break
            await asyncio.sleep(0.01)
        assert job_reporting.reporting_status()["ready"] is True
    finally:
        await job_reporting.stop_readiness_monitor(monitor)
    assert monitor is not None and monitor.done()


async def test_readiness_monitor_survives_unexpected_exception(
    reporting_on, respx_mock, monkeypatch
):
    respx_mock.get(f"{_CSP}/v1/artifact-writer/ready").mock(
        return_value=httpx.Response(
            200,
            json={
                "ready": True,
                "service_id": 1,
                "service_slug": "anila-studio-artifact",
                "service_type": "artifact_tool",
                "data_egress": ["artifact"],
            },
        )
    )
    original_probe = job_reporting.probe_readiness
    calls = 0

    async def _flaky_probe() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("unexpected monitor bug")
        return await original_probe()

    monkeypatch.setattr(job_reporting, "probe_readiness", _flaky_probe)
    monkeypatch.setattr(settings, "STUDIO_ARTIFACT_READINESS_PROBE_SECONDS", 0.01)
    job_reporting.reset_reporting_status()
    monitor = job_reporting.start_readiness_monitor()
    try:
        for _ in range(100):
            if calls >= 2 and job_reporting.reporting_status()["ready"]:
                break
            await asyncio.sleep(0.01)
        assert calls >= 2
        assert job_reporting.reporting_status()["ready"] is True
        assert monitor is not None and not monitor.done()
    finally:
        await job_reporting.stop_readiness_monitor(monitor)


async def test_reporting_refuses_legacy_fallback(monkeypatch):
    monkeypatch.setattr(settings, "STUDIO_ARTIFACT_REPORTING", True)
    monkeypatch.setattr(settings, "STUDIO_ARTIFACT_SERVICE_TOKEN", "")
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "legacy-shared-token")
    job_reporting.reset_reporting_status()

    with pytest.raises(job_reporting.ArtifactReportingError, match="不得回退"):
        await job_reporting.report_job_created(
            bearer="browser-token",
            job_id="no-fallback",
            artifact_type="report",
            status="queued",
            requester_user_id=7,
        )
    assert job_reporting.reporting_status()["ready"] is False


# ---------------------------------------------------------------------------
# Trace spans
# ---------------------------------------------------------------------------


async def test_spans_emitted_when_trace_id_present(reporting_on, respx_mock):
    respx_mock.post(f"{_CSP}/v1/artifact-jobs").mock(return_value=httpx.Response(201))
    respx_mock.post(f"{_CSP}/v1/artifacts/upload").mock(
        return_value=httpx.Response(201, json={"artifact_id": 10, "version_id": 1, "download_url": "/api/artifacts/10/download"})
    )
    respx_mock.patch(url__regex=rf"{_CSP}/v1/artifact-jobs/.*").mock(
        return_value=httpx.Response(200)
    )
    spans = respx_mock.post(f"{_CSP}/v1/traces/trace_777/spans").mock(
        return_value=httpx.Response(202, json={})
    )

    ctx = _make_ctx(trace_id="trace_777")
    await job_lifecycle.on_create(
        _FakeRec(job_id="js", user_id=7, collection_id=5, state="pending", step="queued"),
        ctx,
    )
    await job_lifecycle.on_transition(
        _FakeRec(job_id="js", user_id=7, collection_id=5, state="running", step="rendering"),
        ctx,
        _FakeUpdater(),
    )
    await job_lifecycle.on_transition(
        _FakeRec(job_id="js", user_id=7, collection_id=5, state="done", step="done", title="T"),
        ctx,
        _FakeUpdater(),
    )
    await job_lifecycle.drain()

    assert spans.called
    payload = json.loads(spans.calls.last.request.content)
    span_types = {s["span_type"] for s in payload["spans"]}
    assert "studio.job" in span_types  # root
    assert "studio.stage" in span_types  # per-stage
    assert all(s["producer"] == "studio" for s in payload["spans"])


async def test_no_spans_without_trace_id(reporting_on, respx_mock):
    respx_mock.post(f"{_CSP}/v1/artifact-jobs").mock(return_value=httpx.Response(201))
    respx_mock.post(f"{_CSP}/v1/artifacts/upload").mock(
        return_value=httpx.Response(201, json={"artifact_id": 11, "version_id": 1, "download_url": "/api/artifacts/11/download"})
    )
    respx_mock.patch(url__regex=rf"{_CSP}/v1/artifact-jobs/.*").mock(
        return_value=httpx.Response(200)
    )
    spans = respx_mock.post(url__regex=rf"{_CSP}/v1/traces/.*/spans").mock(
        return_value=httpx.Response(202)
    )
    ctx = _make_ctx(trace_id=None)  # emitter inactive
    await job_lifecycle.on_create(
        _FakeRec(job_id="jn", user_id=7, collection_id=5, state="pending"), ctx
    )
    await job_lifecycle.on_transition(
        _FakeRec(job_id="jn", user_id=7, collection_id=5, state="done", title="T"),
        ctx,
        _FakeUpdater(),
    )
    await job_lifecycle.drain()
    assert not spans.called
