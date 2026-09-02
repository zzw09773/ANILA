"""Batch 2（2026-09-02）：治理回報欄位對齊 csp、簡報落地到 ARTIFACTS_DIR、重啟後仍可下載。"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from fastapi import HTTPException

from app.auth import CurrentUserIdentity
from app.config import settings
from app.services import job_reporting

BASE = settings.CSP_BASE_URL


# ── csp 回報：requester_user_id（int），task_id 是數字字串時轉 int ───────────

@respx.mock
async def test_job_created_report_sends_requester_user_id(monkeypatch):
    monkeypatch.setattr(settings, "STUDIO_ARTIFACT_REPORTING", True, raising=False)
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "csk-x", raising=False)
    route = respx.post(f"{BASE}/v1/artifact-jobs").mock(return_value=httpx.Response(201, json={"job_id": "j1"}))
    await job_reporting.report_job_created(
        bearer="b", job_id="j1", artifact_type="slides", status="queued",
        requester_user_id=42, employee_id=None, task_id="17", source_snapshot_id=None, trace_id="trace-1",
    )
    body = json.loads(route.calls[0].request.content)
    assert body["requester_user_id"] == 42
    assert body["task_id"] == 17
    assert "requester" not in body


@respx.mock
async def test_non_numeric_task_id_is_dropped_not_sent(monkeypatch):
    monkeypatch.setattr(settings, "STUDIO_ARTIFACT_REPORTING", True, raising=False)
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "csk-x", raising=False)
    route = respx.post(f"{BASE}/v1/artifact-jobs").mock(return_value=httpx.Response(201, json={"job_id": "j1"}))
    await job_reporting.report_job_created(
        bearer="b", job_id="j1", artifact_type="slides", status="queued",
        requester_user_id=42, employee_id=None, task_id="task-abc", source_snapshot_id=None, trace_id=None,
    )
    body = json.loads(route.calls[0].request.content)
    assert "task_id" not in body and body["requester_user_id"] == 42


def test_report_context_carries_owner_user_id_as_requester():
    from app.services import job_lifecycle
    ctx = job_lifecycle.make_context(
        artifact_type="slides", owner_user_id=42, requester="alice", bearer="b",
        collection_id=1, describe=None, task_id=None, source_snapshot_id=None, trace_id=None,
    )
    assert ctx.owner_user_id == 42


# ── 落地 + 重啟後下載 ───────────────────────────────────────────────────────

_IDENTITY = CurrentUserIdentity(id=7, username="u", role="user", token_version=0)


async def test_done_slides_job_writes_pptx_to_artifacts_dir(monkeypatch, tmp_path):
    from app.services import studio_job_service as job_mod
    monkeypatch.setattr(settings, "ARTIFACTS_DIR", str(tmp_path))
    job_mod._reset_for_tests()

    async def runner(updater):
        from app.schemas.studio import SlidesSpec
        spec = SlidesSpec(title="t", slides=[{"title": "a", "bullets": ["x"]}])
        await updater.mark_done(spec=spec, pptx_bytes=b"PK-bytes", defects=[], qa_passes=1)

    rec = await job_mod.create_job(user_id=7, collection_id=1, runner=runner, report_ctx=None)
    await job_mod.get_job(rec.job_id).task
    assert (tmp_path / "slides" / f"{rec.job_id}.pptx").read_bytes() == b"PK-bytes"
    job_mod._reset_for_tests()


async def test_pptx_download_falls_back_to_disk_after_restart(monkeypatch, tmp_path):
    from app.api import studio as studio_mod
    from app.services import job_lifecycle
    from app.services import studio_job_service as job_mod
    monkeypatch.setattr(settings, "ARTIFACTS_DIR", str(tmp_path))
    job_mod._reset_for_tests()  # nothing in memory: simulates a restarted studio
    (tmp_path / "slides").mkdir()
    (tmp_path / "slides" / "j_old.pptx").write_bytes(b"PK-old")

    async def fake_read_status(job_id, user_id):
        if user_id != 7:
            return None  # cross-user → the endpoint must 404
        return {"job_id": job_id, "state": "done", "title": "舊簡報"} if job_id == "j_old" else None
    monkeypatch.setattr(job_lifecycle, "read_status", fake_read_status)

    resp = await studio_mod.get_slides_job_pptx("j_old", identity=_IDENTITY)
    body = b"".join([chunk async for chunk in resp.body_iterator])
    assert body == b"PK-old"
    assert "filename*=UTF-8''" in resp.headers["content-disposition"]

    other = CurrentUserIdentity(id=8, username="v", role="user", token_version=0)
    with pytest.raises(HTTPException) as exc:
        await studio_mod.get_slides_job_pptx("j_old", identity=other)
    assert exc.value.status_code == 404
