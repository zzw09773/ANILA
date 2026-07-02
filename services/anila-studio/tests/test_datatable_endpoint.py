"""Endpoint tests for the datatable pipeline.

Covers the four routes end-to-end:
- POST /api/datatables/jobs
- GET  /api/datatables/jobs/{id}
- GET  /api/datatables/jobs/{id}/download/{fmt}
- DELETE /api/datatables/jobs/{id}

Strategy
--------
* Mount the datatable router into a freshly-built FastAPI app so this
  test doesn't depend on main.py being modified (per the build rules).
* Override the auth dependencies so we don't need a real JWT / JWKS /
  Redis revocation cache.
* `respx` intercepts csp HTTP calls (collection lookup + search + LLM
  proxy) so the runner runs the real pipeline against scripted responses.
* `settings.ARTIFACTS_DIR` is pointed at a `tmp_path` so on-disk artifacts
  land in pytest's tempdir, not the system /var.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import datatables as dt_module
from app.auth import (
    CurrentUserIdentity,
    get_bearer_token,
    get_current_user_identity,
)
from app.config import settings
from app.services import datatable_job_service as jobs


_CSP_BASE = settings.CSP_BASE_URL.rstrip("/")


# ── Helpers ───────────────────────────────────────────────────────────────


def _make_identity() -> CurrentUserIdentity:
    return CurrentUserIdentity(
        id=7, username="tester", role="user", token_version=1,
    )


def _build_app() -> FastAPI:
    """Build a minimal FastAPI app with just the datatable router.

    Auth dependencies are overridden so we don't need JWKS / Redis up.
    No lifespan hooks → no jwks_client.start() / revocation_cache.start()
    side effects.
    """
    app = FastAPI()
    app.include_router(dt_module.router)
    app.dependency_overrides[get_current_user_identity] = _make_identity
    app.dependency_overrides[get_bearer_token] = lambda: "dev-bearer"
    return app


def _collection_payload(coll_id: int = 7) -> dict:
    return {
        "id": coll_id,
        "name": "tester-collection",
        "embedding_model": "bge-m3",
        "embedding_dim": 1024,
        "status": "active",
        "created_by": 42,
    }


def _chunk_search_payload() -> dict:
    return {
        "results": [
            {
                "chunk_id": 1,
                "document_id": 100,
                "filename": "report-2026.pdf",
                "chunk_key": "page-3#1",
                "content": (
                    "Q1 客戶數 12345 名,較 Q4 成長 27%。"
                    "全季營收 9,876,543 元,主要來自企業大宗訂單。"
                ),
                "score": 0.91,
                "metadata": {},
                "parent_chunk_id": None,
                "parent_content": None,
                "chunk_type": "leaf",
                "chunk_level": 0,
            }
        ],
    }


def _llm_response_with_spec() -> dict:
    """Build a valid DatatableSpec JSON the LLM proxy returns."""
    spec = {
        "title": "Q1 關鍵指標",
        "subtitle": "客戶與營收摘要",
        "preset": "key_figures",
        "columns": [
            {"key": "metric", "label": "指標", "dtype": "text", "align": "left"},
            {"key": "value", "label": "數值", "dtype": "number", "align": "right"},
            {"key": "rate", "label": "成長率", "dtype": "percent", "align": "right"},
        ],
        "rows": [
            {"cells": {"metric": "客戶數", "value": 12345, "rate": 0.27}},
            {"cells": {"metric": "營收", "value": 9876543, "rate": None}},
        ],
        "notes": "資料來源: report-2026.pdf",
    }
    return {
        "choices": [
            {"message": {"role": "assistant", "content": json.dumps(spec)}}
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 80},
    }


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_artifacts(tmp_path, monkeypatch):
    """Point ARTIFACTS_DIR at the test's tmp dir + clear in-memory jobs.

    The job manager is module-level so different tests in the same
    process would share state. We snapshot+restore around each test.
    """
    monkeypatch.setattr(settings, "ARTIFACTS_DIR", str(tmp_path))
    snapshot = dict(jobs._jobs)
    jobs._jobs.clear()
    yield tmp_path
    jobs._jobs.clear()
    jobs._jobs.update(snapshot)


# ── Tests ─────────────────────────────────────────────────────────────────


def _wait_for_terminal(client: TestClient, job_id: str, timeout_s: float = 10.0) -> dict:
    """Poll GET /jobs/{id} until state ∉ {pending, running}.

    Uses TestClient (which runs the asyncio task on the same loop as the
    request) — pytest-asyncio's `asyncio_mode=auto` means the task
    progresses while we sleep between polls.
    """
    import time

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = client.get(f"/api/datatables/jobs/{job_id}")
        assert r.status_code == 200, r.text
        body = r.json()
        if body["state"] not in ("pending", "running"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish within {timeout_s}s")


def test_post_creates_job_and_runs_full_pipeline(isolated_artifacts):
    """Full pipeline:
       POST /jobs → 202 + pending
         → asyncio task: csp collection → csp search → LLM proxy → exports
       Poll /jobs/{id} until done
       Then GET /download/html, /csv, /xlsx each return 200.
    """
    app = _build_app()
    client = TestClient(app)

    with respx.mock(assert_all_called=False, base_url=_CSP_BASE) as mock:
        # The endpoint pre-flights `get_collection` BEFORE spawning the
        # task, and the task itself calls `get_collection` again at
        # pipeline start — same route, both legs hit it.
        mock.get("/api/ingestion/collections/7").mock(
            return_value=httpx.Response(200, json=_collection_payload(7)),
        )
        mock.post("/api/ingestion/collections/7/search").mock(
            return_value=httpx.Response(200, json=_chunk_search_payload()),
        )
        mock.post("/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_llm_response_with_spec()),
        )

        # POST /jobs.
        r = client.post(
            "/api/datatables/jobs",
            json={
                "collection_id": 7,
                "preset": "key_figures",
                "extra_instructions": "聚焦營收與客戶",
                "target_columns": ["指標", "數值", "成長率"],
                "top_k": 8,
            },
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["state"] == "pending"
        assert body["step"] == "queued"
        job_id = body["job_id"]
        assert job_id.startswith("dt_")

        # Poll to completion.
        final = _wait_for_terminal(client, job_id)
        assert final["state"] == "done", final
        assert final["row_count"] == 2
        assert final["column_count"] == 3
        assert final["title"] == "Q1 關鍵指標"
        assert final["preset"] == "key_figures"
        # download_urls must surface 3 formats.
        urls = final["download_urls"]
        assert set(urls.keys()) == {"html", "csv", "xlsx"}
        assert urls["html"].endswith(f"/jobs/{job_id}/download/html")
        assert urls["csv"].endswith(f"/jobs/{job_id}/download/csv")
        assert urls["xlsx"].endswith(f"/jobs/{job_id}/download/xlsx")

        # All three downloads return 200 with correct mime.
        rh = client.get(urls["html"])
        assert rh.status_code == 200
        assert rh.headers["content-type"].startswith("text/html")
        assert "Q1 關鍵指標" in rh.text

        rc = client.get(urls["csv"])
        assert rc.status_code == 200
        assert rc.headers["content-type"].startswith("text/csv")
        # CSV body (decoded as text) must start with BOM and include
        # the column labels.
        text_body = rc.content.decode("utf-8-sig")
        assert "指標" in text_body
        assert "成長率" in text_body
        # Raw bytes start with EF BB BF (UTF-8 BOM).
        assert rc.content[:3] == b"\xef\xbb\xbf"

        rx = client.get(urls["xlsx"])
        assert rx.status_code == 200
        assert rx.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        # xlsx files start with PK (zip signature).
        assert rx.content[:2] == b"PK"


def test_get_unknown_job_returns_404(isolated_artifacts):
    app = _build_app()
    client = TestClient(app)
    r = client.get("/api/datatables/jobs/dt_does_not_exist")
    assert r.status_code == 404


def test_download_returns_409_while_running(isolated_artifacts):
    """Job in `running` state must yield 409 on download — the artifact
    isn't there yet."""
    app = _build_app()
    client = TestClient(app)

    # Seed a record in pending/running state manually so we don't have to
    # race against the runner.
    from datetime import datetime, timezone
    from pathlib import Path

    from app.services.datatable_job_service import DatatableJobRecord

    rec = DatatableJobRecord(
        job_id="dt_inflight",
        user_id=7,
        collection_id=99,
        state="running",
        step="generating",
        title=None,
        preset=None,
        row_count=None,
        column_count=None,
        error=None,
        artifact_paths={},
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    jobs._jobs[rec.job_id] = rec

    r = client.get("/api/datatables/jobs/dt_inflight/download/html")
    assert r.status_code == 409


def test_download_returns_410_for_failed_job(isolated_artifacts):
    app = _build_app()
    client = TestClient(app)
    from datetime import datetime, timezone

    from app.services.datatable_job_service import DatatableJobRecord

    rec = DatatableJobRecord(
        job_id="dt_failed",
        user_id=7,
        collection_id=99,
        state="failed",
        step=None,
        title=None,
        preset=None,
        row_count=None,
        column_count=None,
        error="LLM 失敗",
        artifact_paths={},
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    jobs._jobs[rec.job_id] = rec

    r = client.get("/api/datatables/jobs/dt_failed/download/csv")
    assert r.status_code == 410
    assert "LLM 失敗" in r.json()["detail"]


def test_download_unknown_fmt_returns_404(isolated_artifacts):
    app = _build_app()
    client = TestClient(app)
    r = client.get("/api/datatables/jobs/anything/download/pdf")
    assert r.status_code == 404


def test_delete_removes_job_and_artifacts(isolated_artifacts):
    """DELETE on a `done` job should clean up the in-memory record AND
    the artifact files on disk."""
    app = _build_app()
    client = TestClient(app)

    from datetime import datetime, timezone

    from app.schemas.datatable import DatatablePreset
    from app.services.datatable_job_service import DatatableJobRecord

    # Plant artifact files on disk and a `done` record pointing to them.
    artifacts_root = isolated_artifacts
    paths = {}
    for fmt in ("html", "csv", "xlsx"):
        p = artifacts_root / f"dt_planted.{fmt}"
        p.write_bytes(b"x")
        paths[fmt] = p

    rec = DatatableJobRecord(
        job_id="dt_planted",
        user_id=7,
        collection_id=1,
        state="done",
        step="done",
        title="planted",
        preset=DatatablePreset.KEY_FIGURES,
        row_count=2,
        column_count=2,
        error=None,
        artifact_paths=paths,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    jobs._jobs[rec.job_id] = rec

    # Sanity: GET works first.
    assert client.get(f"/api/datatables/jobs/{rec.job_id}").status_code == 200

    # DELETE returns 204.
    r = client.delete(f"/api/datatables/jobs/{rec.job_id}")
    assert r.status_code == 204

    # Record gone.
    assert client.get(f"/api/datatables/jobs/{rec.job_id}").status_code == 404
    # Files unlinked.
    for p in paths.values():
        assert not p.exists(), f"{p} should have been deleted"


def test_post_403_when_csp_forbids_collection(isolated_artifacts):
    """The endpoint pre-flights collection access; a csp 403 must
    surface as a synchronous 403 instead of an opaque failed job."""
    app = _build_app()
    client = TestClient(app)

    with respx.mock(assert_all_called=True, base_url=_CSP_BASE) as mock:
        mock.get("/api/ingestion/collections/99").mock(
            return_value=httpx.Response(403, json={"detail": "no access"}),
        )
        r = client.post(
            "/api/datatables/jobs",
            json={"collection_id": 99, "preset": "key_figures"},
        )
        assert r.status_code == 403


def test_cross_user_job_returns_404(isolated_artifacts):
    """A job belonging to user_id != current identity must surface as
    404, not 403 — existence must not leak to a different user."""
    app = _build_app()
    client = TestClient(app)

    from datetime import datetime, timezone

    from app.services.datatable_job_service import DatatableJobRecord

    rec = DatatableJobRecord(
        job_id="dt_someone_else",
        user_id=999,  # NOT 7
        collection_id=1,
        state="done",
        step="done",
        title="not yours",
        preset=None,
        row_count=1,
        column_count=2,
        error=None,
        artifact_paths={},
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    jobs._jobs[rec.job_id] = rec

    assert client.get(f"/api/datatables/jobs/{rec.job_id}").status_code == 404
    assert (
        client.get(f"/api/datatables/jobs/{rec.job_id}/download/html").status_code
        == 404
    )
    assert client.delete(f"/api/datatables/jobs/{rec.job_id}").status_code == 404
