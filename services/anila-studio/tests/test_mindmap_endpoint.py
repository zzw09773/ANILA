"""End-to-end endpoint tests for the Mindmap pipeline.

The test app is built from JUST the mindmaps router (we do NOT touch
``app.main`` because the brief excludes mounting the router there — the
Z stage will wire it). The auth dependencies are overridden so we don't
need to mint real JWTs, and the csp HTTP calls + dot subprocess are
mocked so the tests stay environment-free.

Coverage:
  * POST /jobs validates collection, returns 202 + initial status.
  * GET /jobs/{id} reports running → done transitions while polling.
  * GET /jobs/{id}/download/svg streams the SVG bytes on completion.
  * GET /jobs/{id}/download/dot streams the DOT source for debug.
  * DELETE /jobs/{id} returns 204.
  * Cross-user reads return 404 (no leak between users).
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import mindmaps as mindmaps_mod
from app.auth import (
    CurrentUserIdentity,
    get_bearer_token,
    get_current_user_identity,
)
from app.config import settings
from app.services import mindmap_job_service as jobs


_CSP = settings.CSP_BASE_URL.rstrip("/")


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def test_app(tmp_path, monkeypatch):
    """Build a FastAPI app carrying only the mindmaps router.

    Auth dependencies are overridden to return a fixed identity so the
    handlers can run without a real JWT. The ARTIFACTS_DIR is
    redirected to a tmp dir so disk writes don't escape the test
    sandbox.
    """
    monkeypatch.setattr(settings, "ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    # Reset the in-process job registry so test order doesn't matter.
    jobs._reset_for_tests()

    app = FastAPI()
    app.include_router(mindmaps_mod.router)

    def _override_identity() -> CurrentUserIdentity:
        return CurrentUserIdentity(
            id=42, username="tester", role="user", token_version=1,
        )

    def _override_bearer() -> str:
        return "dev-bearer-token"

    app.dependency_overrides[get_current_user_identity] = _override_identity
    app.dependency_overrides[get_bearer_token] = _override_bearer
    return app


@pytest.fixture
def other_user_app(tmp_path, monkeypatch):
    """A second app where the auth dependency returns a DIFFERENT user.

    Used to verify cross-user job access is rejected (404).
    """
    monkeypatch.setattr(settings, "ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    app = FastAPI()
    app.include_router(mindmaps_mod.router)
    app.dependency_overrides[get_current_user_identity] = (
        lambda: CurrentUserIdentity(
            id=99, username="other", role="user", token_version=1,
        )
    )
    app.dependency_overrides[get_bearer_token] = lambda: "other-bearer"
    return app


def _collection_payload() -> dict:
    return {
        "id": 7,
        "name": "demo-collection",
        "embedding_model": "bge-m3",
        "embedding_dim": 1024,
        "status": "active",
        "created_by": 42,
    }


def _chunk_payload() -> dict:
    return {
        "results": [
            {
                "chunk_id": 1,
                "document_id": 1,
                "filename": "demo.pdf",
                "chunk_key": "page-1#0",
                "content": "RAG 系統由三個元件組成：檢索器、生成器、知識庫。",
                "score": 0.91,
                "metadata": {},
                "parent_chunk_id": None,
                "parent_content": None,
                "chunk_type": "leaf",
                "chunk_level": 0,
            },
        ],
    }


def _mindmap_llm_response() -> dict:
    """OpenAI-shape response. Content is a valid MindmapSpec JSON."""
    spec_json = {
        "title": "RAG 系統概念樹",
        "preset": "concept_tree",
        "layout": "LR",
        "root": {
            "id": "n0",
            "label": "RAG 系統",
            "children": [
                {
                    "id": "n0a",
                    "label": "檢索器",
                    "children": [
                        {"id": "n0a1", "label": "BM25", "children": []},
                        {"id": "n0a2", "label": "Dense", "children": []},
                    ],
                },
                {
                    "id": "n0b",
                    "label": "生成器",
                    "children": [
                        {"id": "n0b1", "label": "LLM 推論", "children": []},
                    ],
                },
                {
                    "id": "n0c",
                    "label": "知識庫",
                    "children": [],
                },
            ],
        },
    }
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(spec_json, ensure_ascii=False),
                },
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 30},
    }


_FAKE_SVG = (
    '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
    '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="400">'
    '<g><text>RAG 系統</text></g></svg>'
).encode("utf-8")


def _patch_dot_subprocess():
    """Context manager that replaces ``create_subprocess_exec`` in the
    renderer module so ``dot`` never runs."""
    fake_proc = AsyncMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(return_value=(_FAKE_SVG, b''))
    return patch(
        "app.services.mindmap_renderer.asyncio.create_subprocess_exec",
        return_value=fake_proc,
    )


def _wait_for_done(client: TestClient, job_id: str, *, attempts: int = 50) -> dict:
    """Poll the status endpoint until state is terminal or attempts hit.

    Returns the last JSON body. The pipeline is mocked so the asyncio
    task completes within ~1 scheduler tick; we still loop to keep the
    test resilient to event-loop ordering quirks.
    """
    last: dict = {}
    for _ in range(attempts):
        resp = client.get(f"/api/mindmaps/jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        last = resp.json()
        if last.get("state") in ("done", "failed", "cancelled"):
            return last
        # Yield the loop so the background task can advance.
        import time
        time.sleep(0.02)
    return last


# ── Tests ───────────────────────────────────────────────────────────────


def test_post_creates_job_and_returns_202(test_app, tmp_path):
    """POST /jobs returns 202 with state=pending and a job_id."""
    with respx.mock(assert_all_called=False, base_url=_CSP) as mock:
        mock.get("/api/ingestion/collections/7").mock(
            return_value=httpx.Response(200, json=_collection_payload()),
        )
        # The runner kicks off in the background; even though we don't
        # wait for it here, csp's search + LLM endpoints might fire
        # before TestClient tears down. Stub them to keep respx happy.
        mock.post("/api/ingestion/collections/7/search").mock(
            return_value=httpx.Response(200, json=_chunk_payload()),
        )
        mock.post("/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_mindmap_llm_response()),
        )

        with _patch_dot_subprocess():
            with TestClient(test_app) as client:
                resp = client.post(
                    "/api/mindmaps/jobs",
                    json={
                        "collection_id": 7,
                        "preset": "concept_tree",
                        "max_depth": 3,
                        "top_k": 5,
                    },
                )

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["job_id"].startswith("m_")
    assert body["state"] in ("pending", "running")  # task may have started
    assert body["preset"] == "concept_tree"


def test_post_404_for_missing_collection(test_app):
    """POST surfaces csp's 404 as a 404 to the caller."""
    with respx.mock(assert_all_called=True, base_url=_CSP) as mock:
        mock.get("/api/ingestion/collections/999").mock(
            return_value=httpx.Response(
                404, json={"detail": "Collection not found"},
            ),
        )
        with TestClient(test_app) as client:
            resp = client.post(
                "/api/mindmaps/jobs",
                json={
                    "collection_id": 999,
                    "preset": "concept_tree",
                },
            )
    assert resp.status_code == 404


def test_full_pipeline_round_trip(test_app, tmp_path):
    """POST → poll → download SVG: the happy path."""
    with respx.mock(assert_all_called=False, base_url=_CSP) as mock:
        mock.get("/api/ingestion/collections/7").mock(
            return_value=httpx.Response(200, json=_collection_payload()),
        )
        mock.post("/api/ingestion/collections/7/search").mock(
            return_value=httpx.Response(200, json=_chunk_payload()),
        )
        mock.post("/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_mindmap_llm_response()),
        )

        with _patch_dot_subprocess():
            with TestClient(test_app) as client:
                create_resp = client.post(
                    "/api/mindmaps/jobs",
                    json={
                        "collection_id": 7,
                        "preset": "concept_tree",
                        "seed_query": "RAG 系統",
                        "max_depth": 3,
                        "top_k": 5,
                    },
                )
                assert create_resp.status_code == 202, create_resp.text
                job_id = create_resp.json()["job_id"]

                final = _wait_for_done(client, job_id)
                assert final["state"] == "done", final
                assert final["title"] == "RAG 系統概念樹"
                assert final["node_count"] == 7  # 1 + 3 + 3 leaves
                assert "svg" in final["download_urls"]
                assert "dot" in final["download_urls"]
                assert "json" in final["download_urls"]

                # SVG download
                svg_resp = client.get(
                    f"/api/mindmaps/jobs/{job_id}/download/svg",
                )
                assert svg_resp.status_code == 200
                assert svg_resp.headers["content-type"].startswith(
                    "image/svg+xml",
                )
                assert svg_resp.content == _FAKE_SVG

                # DOT download
                dot_resp = client.get(
                    f"/api/mindmaps/jobs/{job_id}/download/dot",
                )
                assert dot_resp.status_code == 200
                assert "digraph mindmap" in dot_resp.text
                assert "rankdir=LR" in dot_resp.text

                # JSON spec download — 前端互動式樹狀檢視的資料來源
                json_resp = client.get(
                    f"/api/mindmaps/jobs/{job_id}/download/json",
                )
                assert json_resp.status_code == 200
                assert json_resp.headers["content-type"].startswith(
                    "application/json",
                )
                spec = json_resp.json()
                assert spec["title"] == "RAG 系統概念樹"
                assert spec["root"]["label"]
                # 樹狀結構完整:root 有 children,節點帶 id/label/children
                assert isinstance(spec["root"]["children"], list)
                assert len(spec["root"]["children"]) == 3
                for child in spec["root"]["children"]:
                    assert child["id"] and child["label"]
                    assert isinstance(child["children"], list)


def test_download_json_404_for_legacy_job_without_spec(test_app):
    """升級前完成的 job 沒有 spec_json — fmt=json 回 404,SVG 仍可下載。

    前端 ArtifactViewer 據此 fallback 到舊的「僅提供下載」面板。
    """
    from datetime import datetime, timezone

    from app.services.mindmap_job_service import MindmapJobRecord

    now = datetime.now(timezone.utc)
    rec = MindmapJobRecord(
        job_id="m_legacy_no_json",
        user_id=42,
        collection_id=7,
        preset="concept_tree",
        state="done",
        step=None,
        title="舊版心智圖",
        node_count=3,
        error=None,
        svg_bytes=_FAKE_SVG,
        dot_source="digraph mindmap { a -> b }",
        spec_json=None,
        created_at=now,
        updated_at=now,
    )
    jobs._jobs[rec.job_id] = rec

    with TestClient(test_app) as client:
        json_resp = client.get(
            f"/api/mindmaps/jobs/{rec.job_id}/download/json",
        )
        assert json_resp.status_code == 404
        assert "predates" in json_resp.json()["detail"].lower()

        # SVG 路徑不受影響 — UI fallback 仍能提供下載
        svg_resp = client.get(
            f"/api/mindmaps/jobs/{rec.job_id}/download/svg",
        )
        assert svg_resp.status_code == 200
        assert svg_resp.content == _FAKE_SVG

        status = client.get(f"/api/mindmaps/jobs/{rec.job_id}")
        assert status.status_code == 200
        urls = status.json()["download_urls"]
        assert "svg" in urls
        assert "json" not in urls


def test_get_nonexistent_job_returns_404(test_app):
    with TestClient(test_app) as client:
        resp = client.get("/api/mindmaps/jobs/m_doesnotexist")
    assert resp.status_code == 404


def test_cross_user_access_returns_404(test_app, other_user_app):
    """User A's job MUST NOT leak to User B's GET — they see 404."""
    with respx.mock(assert_all_called=False, base_url=_CSP) as mock:
        mock.get("/api/ingestion/collections/7").mock(
            return_value=httpx.Response(200, json=_collection_payload()),
        )
        mock.post("/api/ingestion/collections/7/search").mock(
            return_value=httpx.Response(200, json=_chunk_payload()),
        )
        mock.post("/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_mindmap_llm_response()),
        )

        with _patch_dot_subprocess():
            with TestClient(test_app) as client_a:
                resp = client_a.post(
                    "/api/mindmaps/jobs",
                    json={"collection_id": 7, "preset": "concept_tree"},
                )
                assert resp.status_code == 202
                job_id = resp.json()["job_id"]

            # User B requests user A's job via the second test app.
            with TestClient(other_user_app) as client_b:
                resp_b = client_b.get(f"/api/mindmaps/jobs/{job_id}")
                assert resp_b.status_code == 404


def test_delete_cancels_pending_job(test_app):
    """DELETE on a not-yet-terminal job returns 204."""
    # Stage the LLM call to take "forever" so the job stays running.
    async def _slow_llm(*args, **kwargs):
        await asyncio.sleep(60)
        return ""

    with respx.mock(assert_all_called=False, base_url=_CSP) as mock:
        mock.get("/api/ingestion/collections/7").mock(
            return_value=httpx.Response(200, json=_collection_payload()),
        )
        mock.post("/api/ingestion/collections/7/search").mock(
            return_value=httpx.Response(200, json=_chunk_payload()),
        )

        with (
            _patch_dot_subprocess(),
            patch(
                "app.api.mindmaps._call_llm_chat",
                side_effect=_slow_llm,
            ),
        ):
            with TestClient(test_app) as client:
                resp = client.post(
                    "/api/mindmaps/jobs",
                    json={"collection_id": 7, "preset": "concept_tree"},
                )
                assert resp.status_code == 202
                job_id = resp.json()["job_id"]
                del_resp = client.delete(f"/api/mindmaps/jobs/{job_id}")
                assert del_resp.status_code == 204


def test_delete_unknown_job_returns_404(test_app):
    with TestClient(test_app) as client:
        resp = client.delete("/api/mindmaps/jobs/m_nope")
    assert resp.status_code == 404


def test_download_unknown_format_returns_422(test_app):
    """FastAPI's Literal type validation should reject e.g. ``png``."""
    with TestClient(test_app) as client:
        # No job created — but the path validator should fire first.
        resp = client.get("/api/mindmaps/jobs/m_anything/download/png")
    # Either 422 (validator caught the bad fmt) or 404 (job missing) is
    # acceptable; both prove the endpoint refuses unknown formats. The
    # FastAPI Literal validator runs before the handler body, so the
    # actual response will be 422.
    assert resp.status_code == 422
