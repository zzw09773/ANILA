"""Tests for the Slice 8b durable job store + CSP reporting + trace spans.

Covers:
  * JobStore round-trip + restart survival (fakeredis, mirroring the
    revocation-cache test's shared FakeServer pattern).
  * All five pipelines registering their job through the store on create.
  * CSP reporting calls (POST /v1/artifact-jobs, PATCH, POST /v1/artifacts)
    with correct payloads incl. task/snapshot/requester passthrough (respx).
  * CSP-down resilience — reporting failures never surface.
  * Trace spans emitted only when a trace_id is present.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import fakeredis.aioredis
import httpx
import pytest
import respx
from pydantic import BaseModel

from app.config import settings
from app.services import job_lifecycle
from app.services.job_lifecycle import ArtifactInfo, JobReportContext
from app.services.job_store import JobStore, PersistedJob, get_job_store



_CSP = settings.CSP_BASE_URL


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


def _persisted(job_id: str = "j1", **over: Any) -> PersistedJob:
    base = dict(
        job_id=job_id,
        artifact_type="slides",
        owner_user_id=7,
        state="done",
        status_view={"job_id": job_id, "state": "done"},
        collection_id=5,
        task_id="17",
        source_snapshot_id="23",
        trace_id="trace_9",
        requester="EMP42",
    )
    base.update(over)
    return PersistedJob(**base)


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
    await s1.put(_persisted(job_id="jr", status_view={"job_id": "jr", "state": "running"}))
    await s1.stop()  # process "dies"

    s2 = JobStore()  # process "restarts": brand-new instance
    await s2.start()
    try:
        got = await s2.get("jr")
        assert got is not None
        assert got.job_id == "jr"
        assert got.status_view == {"job_id": "jr", "state": "running"}
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
    store = get_job_store()
    await store.start()
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
        await store.stop()


# ---------------------------------------------------------------------------
# CSP reporting payloads (respx)
# ---------------------------------------------------------------------------


class _View(BaseModel):
    job_id: str
    state: str
    title: str | None = None
    artifact_id: str | None = None
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
    artifact_id: str | None = None
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


_PPTX = b"PPTX-PRIMARY-BYTES"


def _slides_describe(rec: Any) -> ArtifactInfo:
    return ArtifactInfo(
        artifact_type="slides",
        title=rec.title,
        storage_ref=f"/api/studio/slides/jobs/{rec.job_id}/pptx",
        primary_bytes=_PPTX,
        result_metadata={"slide_count": 3},
    )


def _make_ctx(*, trace_id: str | None = None) -> JobReportContext:
    return JobReportContext(
        artifact_type="slides",
        owner_user_id=7,
        bearer="BEARER-XYZ",
        collection_id=5,
        requester="EMP42",
        task_id="17",
        source_snapshot_id="23",
        trace_id=trace_id,
        describe=_slides_describe,
    )


async def test_csp_reporting_full_cycle(reporting_on, respx_mock):
    created = respx_mock.post(f"{_CSP}/v1/artifact-jobs").mock(
        return_value=httpx.Response(201, json={"job_id": "j1"})
    )
    registered = respx_mock.post(f"{_CSP}/v1/artifacts").mock(
        return_value=httpx.Response(
            201,
            json={"artifact_id": "art_9", "version_id": "v1", "classification_level": "機密"},
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
    # csp's artifact contract: ids are ints, the owner is requester_user_id
    # (+ employee_id for card users). The old string/username body was
    # rejected 422 on every live job (2026-09-02).
    assert create_body["task_id"] == 17
    assert create_body["source_snapshot_id"] == 23
    assert create_body["requester_user_id"] == 7
    assert create_body["employee_id"] == "EMP42"
    assert "requester" not in create_body
    assert created.calls.last.request.headers["Authorization"] == "Bearer BEARER-XYZ"

    # POST /v1/artifacts — storage_ref + content_hash + passthrough.
    assert registered.called
    reg_body = json.loads(registered.calls.last.request.content)
    assert reg_body["artifact_type"] == "slides"
    assert reg_body["storage_ref"].endswith("/pptx")
    assert reg_body["task_id"] == 17
    assert reg_body["source_snapshot_id"] == 23
    assert reg_body["content_hash"] == hashlib.sha256(_PPTX).hexdigest()

    # PATCH /v1/artifact-jobs/{id} — terminal status mapped + artifact_id.
    assert patched.called
    patch_body = json.loads(patched.calls.last.request.content)
    assert patch_body["status"] == "completed"
    assert patch_body["artifact_id"] == "art_9"

    # Registered artifact metadata written back onto the record.
    assert updater.calls
    assert updater.calls[-1]["artifact_id"] == "art_9"
    assert updater.calls[-1]["classification_level"] == "機密"


async def test_csp_reporting_failed_state_patch(reporting_on, respx_mock):
    """A failed job PATCHes status=failed with the error and skips register."""
    respx_mock.post(f"{_CSP}/v1/artifact-jobs").mock(return_value=httpx.Response(201))
    registered = respx_mock.post(f"{_CSP}/v1/artifacts").mock(
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
    assert body["error"] == "boom"


async def test_csp_down_is_swallowed(reporting_on, respx_mock):
    """Every CSP endpoint failing must NOT raise into the caller."""
    respx_mock.post(f"{_CSP}/v1/artifact-jobs").mock(side_effect=httpx.ConnectError("down"))
    respx_mock.post(f"{_CSP}/v1/artifacts").mock(return_value=httpx.Response(503))
    respx_mock.patch(url__regex=rf"{_CSP}/v1/artifact-jobs/.*").mock(
        side_effect=httpx.ConnectError("down")
    )
    ctx = _make_ctx()
    updater = _FakeUpdater()
    # Neither call raises despite CSP being unreachable / erroring.
    await job_lifecycle.on_create(
        _FakeRec(job_id="jd", user_id=7, collection_id=5, state="pending"), ctx
    )
    await job_lifecycle.on_transition(
        _FakeRec(job_id="jd", user_id=7, collection_id=5, state="done", title="D"),
        ctx,
        updater,
    )
    await job_lifecycle.drain()  # completes cleanly
    # register returned nothing usable → no artifact_id write-back.
    assert all("artifact_id" not in c or c["artifact_id"] is None for c in updater.calls)


async def test_trace_id_does_not_post_spans(reporting_on, respx_mock):
    """關聯 id 仍可帶在 artifact job 上，但不再 POST /v1/traces。"""
    created = respx_mock.post(f"{_CSP}/v1/artifact-jobs").mock(
        return_value=httpx.Response(201, json={"job_id": "js"})
    )
    respx_mock.post(f"{_CSP}/v1/artifacts").mock(
        return_value=httpx.Response(201, json={"artifact_id": "a"})
    )
    respx_mock.patch(url__regex=rf"{_CSP}/v1/artifact-jobs/.*").mock(
        return_value=httpx.Response(200)
    )
    spans = respx_mock.post(url__regex=rf"{_CSP}/v1/traces/.*/spans").mock(
        return_value=httpx.Response(202)
    )
    ctx = _make_ctx(trace_id="trace_777")
    await job_lifecycle.on_create(
        _FakeRec(job_id="js", user_id=7, collection_id=5, state="pending", step="queued"),
        ctx,
    )
    await job_lifecycle.on_transition(
        _FakeRec(job_id="js", user_id=7, collection_id=5, state="done", step="done", title="T"),
        ctx,
        _FakeUpdater(),
    )
    await job_lifecycle.drain()
    assert not spans.called
    body = json.loads(created.calls[0].request.content)
    assert body.get("trace_id") == "trace_777"
