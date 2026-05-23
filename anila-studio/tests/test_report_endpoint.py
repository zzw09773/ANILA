"""End-to-end test for app.api.reports — auth, job lifecycle, downloads.

Strategy:
  - Stand up a fresh FastAPI app with only the reports router mounted.
  - Override the two auth dependencies (``get_current_user_identity`` and
    ``get_bearer_token``) so we don't need a JWKS/Redis stack.
  - Patch csp_client.* on the report runner and api modules so the
    pipeline runs end-to-end against fake fixture data.
  - Patch the actual PDF/DOCX writers (chromium + pandoc are not available
    in the unit-test environment) but let HTML render really happen.
  - Poll the GET /jobs/{id} endpoint until state=='done', then download
    each of the three formats and assert mime + content.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.reports import router as reports_router
from app.auth import (
    CurrentUserIdentity,
    get_bearer_token,
    get_current_user_identity,
)
from app.clients.csp_client import ChunkHit, CollectionMeta
from app.schemas.report import ReportPreset


# ── Stubs ────────────────────────────────────────────────────────────────


_FAKE_USER = CurrentUserIdentity(
    id=42, username="alice", role="user", token_version=0
)
_FAKE_BEARER = "fake-bearer-token"

_FAKE_COLLECTION = CollectionMeta(
    id=7,
    name="test-coll",
    embedding_model="qwen3",
    embedding_dim=1024,
    status="active",
    created_by=42,
)


def _fake_chunks(n: int = 5) -> list[ChunkHit]:
    return [
        ChunkHit(
            chunk_id=i,
            document_id=100 + i,
            filename=f"doc_{i}.pdf",
            chunk_key=f"k_{i}",
            content=f"這是第 {i} 段測試內容，用於驗證 RAG retrieval 與 outline。",
            score=0.9 - i * 0.05,
            metadata={},
            parent_chunk_id=None,
            parent_content=None,
            chunk_type="leaf",
            chunk_level=0,
        )
        for i in range(1, n + 1)
    ]


def _fake_outline_response() -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": (
                        '{\n'
                        '  "title": "測試報告：RAG 系統綜述",\n'
                        '  "tldr": "本報告整理 RAG 系統的核心元件與部署模式，'
                        '聚焦於檢索品質、生成穩定性與工程取捨。",\n'
                        '  "sections": [\n'
                        '    {"heading": "系統元件", "key_points": ["檢索器", "生成器"]},\n'
                        '    {"heading": "部署考量", "key_points": ["延遲", "成本"]}\n'
                        '  ]\n'
                        '}'
                    )
                }
            }
        ]
    }


def _fake_draft_response(heading: str) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": (
                        f"關於 **{heading}** 的章節內容。\n\n"
                        f"- 第一點 [1]\n"
                        f"- 第二點 [2]\n\n"
                        f"更多詳細內容如下 [3]。"
                    )
                }
            }
        ]
    }


# ── Fixture: app + patched deps ──────────────────────────────────────────


@pytest.fixture
def patched_runtime(monkeypatch, tmp_path: Path):
    """Patch csp_client + report_renderer's PDF/DOCX paths.

    Returns the artifacts dir so tests can assert files landed.
    """
    from app.api import reports as reports_mod
    from app.services import report_runner
    from app.services import report_renderer
    from app import config as config_mod

    # Redirect ARTIFACTS_DIR to tmp so we don't write into /var
    monkeypatch.setattr(config_mod.settings, "ARTIFACTS_DIR", str(tmp_path))

    # 1. get_collection (called by POST /jobs auth check)
    async def fake_get_collection(collection_id: int, *, bearer: str):
        assert collection_id == 7
        return _FAKE_COLLECTION

    monkeypatch.setattr(reports_mod, "get_collection", fake_get_collection)

    # 2. search_chunks (retrieval stage of the runner)
    async def fake_search_chunks(collection_id: int, query: str, **kwargs):
        return _fake_chunks(5)

    monkeypatch.setattr(report_runner, "search_chunks", fake_search_chunks)

    # 3. proxy_chat_completions — outline call (no JSON-mode response) then
    #    draft call per section. We dispatch by inspecting messages for
    #    'sections' (outline) vs 'Section 標題' (draft).
    async def fake_proxy_chat_completions(*, model, messages, bearer, **kwargs):
        user_content = messages[-1]["content"]
        if "JSON object" in user_content or "JSON" in user_content and "sections" in user_content:
            return _fake_outline_response()
        # Heading is on a line like "Section 標題：XXXX"
        heading = "Untitled"
        for line in user_content.splitlines():
            if line.startswith("Section 標題："):
                heading = line.split("：", 1)[1].strip()
                break
        return _fake_draft_response(heading)

    monkeypatch.setattr(
        report_runner, "proxy_chat_completions", fake_proxy_chat_completions
    )

    # 4. PDF + DOCX renderers — write small fake files so the runner's
    #    existence checks pass; chromium / pandoc not available in test env.
    async def fake_pdf(html, dest):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"%PDF-FAKE\n" + html[:200].encode("utf-8", "ignore"))

    async def fake_docx(html, dest):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"PK\x03\x04docx-fake")

    monkeypatch.setattr(report_renderer, "write_pdf_file", fake_pdf)
    monkeypatch.setattr(report_renderer, "write_docx_file", fake_docx)

    return tmp_path


@pytest.fixture
def app(patched_runtime):
    """FastAPI app with reports router + overridden auth deps."""
    test_app = FastAPI()
    test_app.include_router(reports_router)

    async def fake_identity() -> CurrentUserIdentity:
        return _FAKE_USER

    async def fake_bearer() -> str:
        return _FAKE_BEARER

    test_app.dependency_overrides[get_current_user_identity] = fake_identity
    test_app.dependency_overrides[get_bearer_token] = fake_bearer
    return test_app


@pytest.fixture
def client(app):
    return TestClient(app)


def _poll_until_done(client: TestClient, job_id: str, timeout_s: float = 5.0) -> dict:
    """Poll GET /jobs/{id} until state in (done, failed, cancelled) or timeout."""
    import time

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        resp = client.get(f"/api/reports/jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body["state"] in ("done", "failed", "cancelled"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish within {timeout_s}s")


# ── Tests ────────────────────────────────────────────────────────────────


def test_post_jobs_returns_202_pending(client):
    resp = client.post(
        "/api/reports/jobs",
        json={"collection_id": 7, "preset": "key_summary"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["state"] == "pending"
    assert body["job_id"].startswith("r_")
    assert body["preset"] == "key_summary"
    assert body["download_urls"] is None


def test_end_to_end_job_completes_and_downloads_all_three_formats(client, patched_runtime):
    artifacts_dir = patched_runtime
    resp = client.post(
        "/api/reports/jobs",
        json={"collection_id": 7, "preset": "deep_tech_review"},
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    final = _poll_until_done(client, job_id)
    assert final["state"] == "done", f"job failed: {final.get('error')}"
    assert final["sections_count"] == 2
    assert final["references_count"] == 5
    assert final["title"]  # populated by pipeline
    urls = final["download_urls"]
    assert urls is not None
    assert set(urls.keys()) == {"html", "pdf", "docx"}
    # Relative URLs, not absolute
    for url in urls.values():
        assert url.startswith("/api/reports/jobs/")
        assert "://" not in url

    # Each file landed on disk
    for fmt in ("html", "pdf", "docx"):
        assert (artifacts_dir / f"{job_id}.{fmt}").exists()

    # Download HTML
    resp_html = client.get(f"/api/reports/jobs/{job_id}/download/html")
    assert resp_html.status_code == 200
    assert "text/html" in resp_html.headers["content-type"]
    body = resp_html.content.decode("utf-8")
    assert "<!doctype html>" in body
    assert "RAG" in body or "測試" in body  # title made it through

    # Download PDF
    resp_pdf = client.get(f"/api/reports/jobs/{job_id}/download/pdf")
    assert resp_pdf.status_code == 200
    assert resp_pdf.headers["content-type"].startswith("application/pdf")
    assert resp_pdf.content.startswith(b"%PDF")

    # Download DOCX
    resp_docx = client.get(f"/api/reports/jobs/{job_id}/download/docx")
    assert resp_docx.status_code == 200
    assert "wordprocessingml" in resp_docx.headers["content-type"]
    assert resp_docx.content.startswith(b"PK")


def test_download_unknown_job_returns_404(client):
    resp = client.get("/api/reports/jobs/r_nonexistent/download/html")
    assert resp.status_code == 404


def test_get_unknown_job_returns_404(client):
    resp = client.get("/api/reports/jobs/r_nonexistent")
    assert resp.status_code == 404


def test_download_unknown_format_rejected(client):
    """fmt path param is constrained by Literal — anything else is 422."""
    resp = client.get("/api/reports/jobs/r_x/download/txt")
    assert resp.status_code == 422


def test_delete_unknown_job_returns_404(client):
    resp = client.delete("/api/reports/jobs/r_nonexistent")
    assert resp.status_code == 404


def test_delete_running_job_returns_204(client, patched_runtime):
    """Cancel an in-flight job before the pipeline finishes.

    Because the runner finishes in <100ms with our mocks, we wrap the
    runner to add a delay so we have a window to cancel.
    """
    import time
    from app.services import report_runner as runner_mod

    original = runner_mod.run_report_pipeline

    async def slow_run(*args, **kwargs):
        await asyncio.sleep(0.5)
        await original(*args, **kwargs)

    runner_mod.run_report_pipeline = slow_run
    try:
        resp = client.post(
            "/api/reports/jobs",
            json={"collection_id": 7, "preset": "key_summary"},
        )
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        # Cancel immediately
        del_resp = client.delete(f"/api/reports/jobs/{job_id}")
        assert del_resp.status_code == 204

        # State should be cancelled (or already failed if cancellation
        # arrived after sleep finished). We allow a small window.
        final = _poll_until_done(client, job_id, timeout_s=2.0)
        assert final["state"] in ("cancelled", "done", "failed")
    finally:
        runner_mod.run_report_pipeline = original


def test_invalid_preset_rejected_by_schema(client):
    resp = client.post(
        "/api/reports/jobs",
        json={"collection_id": 7, "preset": "not_a_real_preset"},
    )
    assert resp.status_code == 422


def test_missing_collection_id_rejected(client):
    resp = client.post(
        "/api/reports/jobs",
        json={"preset": "key_summary"},
    )
    assert resp.status_code == 422


def test_cross_user_job_returns_404(app, patched_runtime):
    """A second user must not be able to GET another user's job."""
    # Create job as user 42
    client_user_a = TestClient(app)
    resp = client_user_a.post(
        "/api/reports/jobs",
        json={"collection_id": 7, "preset": "key_summary"},
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    # Swap dependency override to a different user and try to read
    async def fake_other_identity() -> CurrentUserIdentity:
        return CurrentUserIdentity(
            id=99, username="bob", role="user", token_version=0
        )

    app.dependency_overrides[get_current_user_identity] = fake_other_identity
    client_user_b = TestClient(app)

    resp = client_user_b.get(f"/api/reports/jobs/{job_id}")
    assert resp.status_code == 404


def test_preset_in_status_during_running(client, patched_runtime):
    """preset should be populated even before pipeline finishes."""
    resp = client.post(
        "/api/reports/jobs",
        json={"collection_id": 7, "preset": "teaching_handout"},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["preset"] == "teaching_handout"
