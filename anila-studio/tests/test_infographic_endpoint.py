"""Endpoint integration tests — full job lifecycle with mocked external deps.

Uses ``app.dependency_overrides`` to bypass JWKS/JWT verification — auth
is exercised in ``test_auth.py``; here we only care about the infographic
pipeline wiring.

Why ``httpx.AsyncClient`` instead of ``TestClient``:
  TestClient creates a fresh event loop per request, which cancels any
  ``asyncio.create_task`` spawned by the previous request. The job
  service spawns the pipeline on a background task, so polling via
  TestClient would always see ``state="cancelled"``. AsyncClient with
  ASGITransport keeps one loop across all calls, which is what we need.

External calls mocked:
- ``get_collection`` / ``search_chunks`` (csp HTTP)
- ``proxy_chat_completions`` (LLM)
- ``render_pdf`` (Playwright headless chromium) — we don't want to spin
  a real browser in unit tests.
"""
from __future__ import annotations

import asyncio
import json
import pathlib

import httpx
import pytest

from fastapi import FastAPI

from app.api.infographics import router as infographics_router
from app.auth import (
    CurrentUserIdentity,
    get_bearer_token,
    get_current_user_identity,
)
from app.clients.csp_client import ChunkHit, CollectionMeta


_DUMMY_USER = CurrentUserIdentity(
    id=42, username="alice", role="user", token_version=0,
)


def _stub_get_current_user_identity():
    return _DUMMY_USER


def _stub_get_bearer_token():
    return "test-bearer-token"


@pytest.fixture
def artifacts_dir(tmp_path, monkeypatch):
    """Point ARTIFACTS_DIR at a per-test tmp directory."""
    from app.config import settings as settings_mod

    monkeypatch.setattr(settings_mod, "ARTIFACTS_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def app(artifacts_dir):
    test_app = FastAPI()
    test_app.include_router(infographics_router)
    test_app.dependency_overrides[get_current_user_identity] = (
        _stub_get_current_user_identity
    )
    test_app.dependency_overrides[get_bearer_token] = _stub_get_bearer_token
    return test_app


@pytest.fixture
async def client(app):
    """httpx AsyncClient over ASGITransport — survives across calls so
    spawned asyncio tasks aren't cancelled between requests."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver",
    ) as ac:
        yield ac


@pytest.fixture(autouse=True)
def reset_jobs_registry():
    """Make sure no jobs leak between tests."""
    from app.services import infographic_job_service as job_mod

    job_mod._jobs.clear()
    yield
    job_mod._jobs.clear()


# ── LLM response fixture ──────────────────────────────────────────────────


_GOOD_SPEC_JSON = json.dumps({
    "title": "2024 Q4 業務簡報",
    "subtitle": "董事會月度報告",
    "preset": "mission_dashboard",
    "stats": [
        {"value": "47%", "label": "目標達成率", "delta": "+12%"},
        {"value": "3.5×", "label": "效能提升"},
    ],
    "charts": [
        {
            "chart_type": "bar",
            "title": "各部門達成率",
            "x_labels": ["業務", "工程", "客服"],
            "series": [{"name": "達成率", "values": [88, 92, 75]}],
        },
    ],
    "comparison": [
        {"label": "可靠性", "columns": ["A 方案：高", "B 方案：中"]},
    ],
    "timeline": [
        {"date": "2025-Q1", "title": "啟動"},
    ],
    "takeaway": "本季關鍵指標達成預期。",
})


# ── Async mock helpers ────────────────────────────────────────────────────


def _patch_csp(monkeypatch, *, chunks=None, collection_status="active"):
    """Replace csp_client calls used by the infographics module + the
    POST handler authz check (which also calls get_collection directly)."""
    from app.api import infographics as ig_mod

    async def fake_get_collection(collection_id, *, bearer):
        return CollectionMeta(
            id=collection_id,
            name="測試知識庫",
            embedding_model="dummy",
            embedding_dim=512,
            status=collection_status,
            created_by=1,
        )

    async def fake_search_chunks(collection_id, query, **kwargs):
        return chunks or [
            ChunkHit(
                chunk_id=1,
                document_id=10,
                filename="report_q4.pdf",
                chunk_key="c-0001",
                content="業績達成率 47%，較上季 +12 個百分點。",
                score=0.92,
                metadata={},
                parent_chunk_id=None,
                parent_content=None,
            ),
        ]

    monkeypatch.setattr(ig_mod, "get_collection", fake_get_collection)
    monkeypatch.setattr(ig_mod, "search_chunks", fake_search_chunks)


def _patch_llm(monkeypatch, *, response_text=_GOOD_SPEC_JSON):
    from app.api import infographics as ig_mod

    async def fake_proxy_chat_completions(*, model, messages, **kwargs):
        return {
            "choices": [
                {"message": {"role": "assistant", "content": response_text}}
            ]
        }

    monkeypatch.setattr(
        ig_mod, "proxy_chat_completions", fake_proxy_chat_completions,
    )


def _patch_pdf(monkeypatch):
    """Stub render_pdf to write a placeholder file instead of spinning
    a real Playwright chromium."""
    from app.api import infographics as ig_mod

    async def fake_render_pdf(html, dest_path):
        dest_path = pathlib.Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(b"%PDF-1.4\n%fake pdf for tests\n")

    monkeypatch.setattr(ig_mod, "render_pdf", fake_render_pdf)


# ── Lifecycle helpers ─────────────────────────────────────────────────────


async def _wait_for_state(client, job_id, *, target=("done", "failed"), timeout=10.0):
    """Poll /jobs/{id} until state is terminal or timeout (async version)."""
    deadline = asyncio.get_event_loop().time() + timeout
    last = None
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/api/infographics/jobs/{job_id}")
        if resp.status_code != 200:
            return resp
        last = resp.json()
        if last["state"] in target:
            return resp
        await asyncio.sleep(0.05)
    raise AssertionError(f"Job {job_id} did not reach {target} (last={last})")


# ── Tests ─────────────────────────────────────────────────────────────────


async def test_create_job_returns_202_with_pending_status(
    monkeypatch, client, artifacts_dir,
):
    _patch_csp(monkeypatch)
    _patch_llm(monkeypatch)
    _patch_pdf(monkeypatch)

    resp = await client.post(
        "/api/infographics/jobs",
        json={"collection_id": 1, "preset": "mission_dashboard"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["state"] == "pending"
    assert body["job_id"].startswith("ig_")
    assert body["preset"] == "mission_dashboard"


async def test_full_pipeline_reaches_done_state(
    monkeypatch, client, artifacts_dir,
):
    _patch_csp(monkeypatch)
    _patch_llm(monkeypatch)
    _patch_pdf(monkeypatch)

    create_resp = await client.post(
        "/api/infographics/jobs",
        json={"collection_id": 1, "preset": "mission_dashboard"},
    )
    assert create_resp.status_code == 202
    job_id = create_resp.json()["job_id"]

    final = await _wait_for_state(client, job_id)
    body = final.json()
    assert body["state"] == "done", body
    assert body["title"] == "2024 Q4 業務簡報"
    assert body["chart_count"] == 1
    assert body["download_urls"] is not None
    assert body["download_urls"]["html"].endswith("/download/html")
    assert body["download_urls"]["pdf"].endswith("/download/pdf")

    # Files exist on disk
    assert (artifacts_dir / f"{job_id}.html").exists()
    assert (artifacts_dir / f"{job_id}.pdf").exists()


async def test_html_download_returns_html_content(
    monkeypatch, client, artifacts_dir,
):
    _patch_csp(monkeypatch)
    _patch_llm(monkeypatch)
    _patch_pdf(monkeypatch)

    create_resp = await client.post(
        "/api/infographics/jobs",
        json={"collection_id": 1, "preset": "comparison_matrix"},
    )
    job_id = create_resp.json()["job_id"]
    await _wait_for_state(client, job_id)

    html_resp = await client.get(f"/api/infographics/jobs/{job_id}/download/html")
    assert html_resp.status_code == 200
    assert html_resp.headers["content-type"].startswith("text/html")
    text = html_resp.text
    assert text.startswith("<!DOCTYPE html>")
    assert "2024 Q4 業務簡報" in text
    assert "47%" in text


async def test_pdf_download_returns_pdf_content(
    monkeypatch, client, artifacts_dir,
):
    _patch_csp(monkeypatch)
    _patch_llm(monkeypatch)
    _patch_pdf(monkeypatch)

    create_resp = await client.post(
        "/api/infographics/jobs",
        json={"collection_id": 1, "preset": "stats_brief"},
    )
    job_id = create_resp.json()["job_id"]
    await _wait_for_state(client, job_id)

    pdf_resp = await client.get(f"/api/infographics/jobs/{job_id}/download/pdf")
    assert pdf_resp.status_code == 200
    assert pdf_resp.headers["content-type"].startswith("application/pdf")
    assert pdf_resp.content.startswith(b"%PDF-")


async def test_download_unknown_fmt_returns_400(
    monkeypatch, client, artifacts_dir,
):
    _patch_csp(monkeypatch)
    _patch_llm(monkeypatch)
    _patch_pdf(monkeypatch)

    create_resp = await client.post(
        "/api/infographics/jobs",
        json={"collection_id": 1, "preset": "stats_brief"},
    )
    job_id = create_resp.json()["job_id"]
    await _wait_for_state(client, job_id)

    resp = await client.get(f"/api/infographics/jobs/{job_id}/download/docx")
    assert resp.status_code == 400


async def test_get_unknown_job_returns_404(client):
    resp = await client.get("/api/infographics/jobs/ig_does_not_exist")
    assert resp.status_code == 404


async def test_delete_unknown_job_returns_404(client):
    resp = await client.delete("/api/infographics/jobs/ig_does_not_exist")
    assert resp.status_code == 404


async def test_delete_done_job_cleans_artifacts(
    monkeypatch, client, artifacts_dir,
):
    _patch_csp(monkeypatch)
    _patch_llm(monkeypatch)
    _patch_pdf(monkeypatch)

    create_resp = await client.post(
        "/api/infographics/jobs",
        json={"collection_id": 1, "preset": "stats_brief"},
    )
    job_id = create_resp.json()["job_id"]
    await _wait_for_state(client, job_id)

    html_path = artifacts_dir / f"{job_id}.html"
    pdf_path = artifacts_dir / f"{job_id}.pdf"
    assert html_path.exists()
    assert pdf_path.exists()

    del_resp = await client.delete(f"/api/infographics/jobs/{job_id}")
    assert del_resp.status_code == 204

    # Files cleaned up
    assert not html_path.exists()
    assert not pdf_path.exists()

    # Job record gone — subsequent GET 404
    get_resp = await client.get(f"/api/infographics/jobs/{job_id}")
    assert get_resp.status_code == 404


async def test_create_with_invalid_collection_id_returns_422(client):
    """Pydantic-level validation: collection_id must be >= 1."""
    resp = await client.post(
        "/api/infographics/jobs",
        json={"collection_id": 0, "preset": "stats_brief"},
    )
    assert resp.status_code == 422


async def test_create_with_invalid_preset_returns_422(client):
    resp = await client.post(
        "/api/infographics/jobs",
        json={"collection_id": 1, "preset": "not_a_preset"},
    )
    assert resp.status_code == 422


async def test_create_with_csp_not_found_returns_404(monkeypatch, client):
    """When the up-front authz check sees CspNotFoundError, return 404."""
    from app.api import infographics as ig_mod
    from app.clients.csp_client import CspNotFoundError

    async def boom_get_collection(*args, **kwargs):
        raise CspNotFoundError("collection 999 not found")

    monkeypatch.setattr(ig_mod, "get_collection", boom_get_collection)

    resp = await client.post(
        "/api/infographics/jobs",
        json={"collection_id": 999, "preset": "stats_brief"},
    )
    assert resp.status_code == 404


async def test_llm_invalid_json_lands_job_in_failed_state(
    monkeypatch, client, artifacts_dir,
):
    """If LLM returns garbage AND correction pass also fails, job fails."""
    _patch_csp(monkeypatch)
    _patch_llm(monkeypatch, response_text="this is not JSON at all")
    _patch_pdf(monkeypatch)

    create_resp = await client.post(
        "/api/infographics/jobs",
        json={"collection_id": 1, "preset": "stats_brief"},
    )
    job_id = create_resp.json()["job_id"]
    final = await _wait_for_state(client, job_id, target=("done", "failed"))
    body = final.json()
    assert body["state"] == "failed", body
    assert body["error"] is not None
