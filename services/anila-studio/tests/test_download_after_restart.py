"""重啟／快取淘汰後，四種產出的下載要從持久化紀錄 + 磁碟檔案復原，權限檢查不變。"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.auth import CurrentUserIdentity
from app.config import settings
from app.services import job_lifecycle
from app.services.job_store import PersistedJob

OWNER = CurrentUserIdentity(id=7, username="u", role="user", token_version=0)
OTHER = CurrentUserIdentity(id=8, username="v", role="user", token_version=0)


class _FakeStore:
    def __init__(self, rows):
        self.rows = {r.job_id: r for r in rows}

    async def get(self, job_id):
        return self.rows.get(job_id)


def _job(job_id, artifact_type, state="done", **view):
    return PersistedJob(
        job_id=job_id, artifact_type=artifact_type, owner_user_id=7, state=state,
        status_view={"job_id": job_id, "state": state, "title": "季報", **view},
    )


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ARTIFACTS_DIR", str(tmp_path))
    fake = _FakeStore([])
    monkeypatch.setattr(job_lifecycle, "get_job_store", lambda: fake)
    return fake, tmp_path


async def _body(resp):
    if hasattr(resp, "body_iterator"):
        return b"".join([c async for c in resp.body_iterator])
    with open(resp.path, "rb") as fh:
        return fh.read()


async def test_report_download_after_memory_cleared(store):
    from app.api import reports
    from app.services import report_job_service as jobs
    fake, root = store
    jobs._jobs.clear()
    fake.rows["r_old"] = _job("r_old", "report", download_urls={"html": "/x.html"})
    (root / "r_old.html").write_bytes(b"<html>ok</html>")
    resp = await reports.download_report("r_old", "html", identity=OWNER)
    assert await _body(resp) == b"<html>ok</html>"
    for ident, jid in ((OTHER, "r_old"), (OWNER, "missing")):
        with pytest.raises(HTTPException) as exc:
            await reports.download_report(jid, "html", identity=ident)
        assert exc.value.status_code == 404


async def test_report_running_and_failed_after_restart(store):
    from app.api import reports
    fake, _ = store
    fake.rows["r_run"] = _job("r_run", "report", state="running")
    fake.rows["r_bad"] = _job("r_bad", "report", state="failed", error="boom")
    with pytest.raises(HTTPException) as exc:
        await reports.download_report("r_run", "html", identity=OWNER)
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        await reports.download_report("r_bad", "html", identity=OWNER)
    assert exc.value.status_code == 410


async def test_wrong_artifact_type_is_404(store):
    from app.api import reports
    fake, root = store
    fake.rows["m_1"] = _job("m_1", "mindmap", download_urls={"html": "/x"})
    (root / "m_1.html").write_bytes(b"x")
    with pytest.raises(HTTPException) as exc:
        await reports.download_report("m_1", "html", identity=OWNER)
    assert exc.value.status_code == 404


async def test_mindmap_download_after_memory_cleared(store):
    from app.api import mindmaps
    fake, root = store
    fake.rows["m_old"] = _job("m_old", "mindmap")
    (root / "m_old.svg").write_bytes(b"<svg/>")
    resp = await mindmaps.download_mindmap("m_old", "svg", identity=OWNER)
    assert await _body(resp) == b"<svg/>"
    with pytest.raises(HTTPException) as exc:
        await mindmaps.download_mindmap("m_old", "svg", identity=OTHER)
    assert exc.value.status_code == 404


async def test_infographic_download_after_memory_cleared(store):
    from app.api import infographics
    fake, root = store
    fake.rows["i_old"] = _job("i_old", "infographic")
    (root / "i_old.pdf").write_bytes(b"%PDF")
    resp = await infographics.download_infographic_artifact("i_old", "pdf", identity=OWNER)
    assert await _body(resp) == b"%PDF"
    with pytest.raises(HTTPException) as exc:
        await infographics.download_infographic_artifact("i_old", "pdf", identity=OTHER)
    assert exc.value.status_code == 404


async def test_datatable_download_after_memory_cleared(store):
    from app.api import datatables
    fake, root = store
    fake.rows["d_old"] = _job("d_old", "datatable")
    (root / "datatables").mkdir()
    (root / "datatables" / "d_old.csv").write_bytes(b"a,b\n")
    resp = await datatables.download_datatable_artifact("d_old", "csv", identity=OWNER)
    assert await _body(resp) == b"a,b\n"
    with pytest.raises(HTTPException) as exc:
        await datatables.download_datatable_artifact("d_old", "csv", identity=OTHER)
    assert exc.value.status_code == 404


async def test_datatable_real_overflow_eviction_keeps_files_downloadable(store):
    """The actual eviction path must not delete files the fallback serves."""
    from datetime import datetime, timedelta, timezone

    from app.api import datatables
    from app.schemas.datatable import DatatablePreset
    from app.services import datatable_job_service as jobs

    fake, root = store
    (root / "datatables").mkdir()
    jobs._jobs.clear()
    base = datetime.now(timezone.utc)
    for i in range(jobs.MAX_JOBS_PER_USER + 1):
        jid = f"dt_{i:02d}"
        paths = {ext: root / "datatables" / f"{jid}.{ext}" for ext in ("html", "csv", "xlsx")}
        for p in paths.values():
            p.write_bytes(f"{jid}".encode())
        jobs._jobs[jid] = jobs.DatatableJobRecord(
            job_id=jid, user_id=7, collection_id=1, state="done", step="done",
            title="t", preset=DatatablePreset.KEY_FIGURES, row_count=1,
            column_count=1, error=None, artifact_paths=paths,
            created_at=base + timedelta(seconds=i), updated_at=base,
        )
        fake.rows[jid] = _job(jid, "datatable")
    jobs._evict_user_overflow(7)
    assert "dt_00" not in jobs._jobs
    resp = await datatables.download_datatable_artifact("dt_00", "csv", identity=OWNER)
    assert await _body(resp) == b"dt_00"
    jobs._jobs.clear()
