"""接受工作時換成 CSP 的工作權杖。之後使用者存取權杖過期，工作仍完成。"""
from __future__ import annotations

import json

import httpx
import pytest
import respx
from fastapi import FastAPI

from app.auth import CurrentUserIdentity, get_bearer_token, get_current_user_identity
from app.config import settings
from app.schemas.studio import Slide, SlidesSpec


_USER = "user-token"
_JOB = "job-token"
_IDENTITY = CurrentUserIdentity(id=7, username="alice", role="user", token_version=3)

_COLLECTION = {
    "id": 1,
    "name": "kb",
    "embedding_model": "embed",
    "embedding_dim": 8,
    "status": "active",
    "created_by": 7,
}


def _app() -> FastAPI:
    from app.api.studio import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user_identity] = lambda: _IDENTITY
    app.dependency_overrides[get_bearer_token] = lambda: _USER
    return app


@pytest.fixture
def artifacts(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "STUDIO_ARTIFACT_REPORTING", True)
    return tmp_path


async def test_slides_job_succeeds_after_user_token_expires(artifacts, monkeypatch):
    """預檢仍用使用者權杖；之後的 CSP 呼叫改用工權杖，過期的使用者權杖不再被送出。"""
    from app.api import studio as studio_mod
    from app.services import job_lifecycle, studio_job_service as jobs

    user_reads = {"n": 0}
    later = []

    def _collection(request: httpx.Request) -> httpx.Response:
        auth = request.headers["authorization"]
        if auth == f"Bearer {_USER}":
            user_reads["n"] += 1
            if user_reads["n"] > 1:
                return httpx.Response(401, json={"detail": "權杖已過期"})
            return httpx.Response(200, json=_COLLECTION)
        if auth == f"Bearer {_JOB}":
            return httpx.Response(200, json=_COLLECTION)
        return httpx.Response(401, json={"detail": "未知權杖"})

    def _mint(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {_USER}"
        body = json.loads(request.content)
        assert body["collection_id"] == 1
        assert str(body["job_id"]).startswith("j_")
        return httpx.Response(
            200,
            json={"token": _JOB, "token_type": "bearer", "expires_in": 3600},
        )

    def _record(request: httpx.Request) -> httpx.Response:
        later.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.method == "POST" and request.url.path.rstrip("/").endswith("/v1/artifacts"):
            return httpx.Response(
                201,
                json={
                    "artifact_id": "art_1",
                    "version_id": "v1",
                    "classification_level": "無機密",
                },
            )
        return httpx.Response(201, json={"job_id": "csp-job"})

    async def fake_pipeline(*, identity, bearer, payload, updater):
        from app.clients.csp_client import get_collection

        await get_collection(payload.collection_id, bearer=bearer)
        spec = SlidesSpec(
            title="還在",
            theme="official",
            slides=[Slide(title="一頁", bullets=["內容還在"])],
        )
        await updater.mark_done(
            spec=spec, pptx_bytes=b"PK-deck", defects=[], qa_passes=0,
        )

    monkeypatch.setattr(studio_mod, "_run_pipeline", fake_pipeline)
    jobs._reset_for_tests()

    with respx.mock(assert_all_called=False, base_url=settings.CSP_BASE_URL) as mock:
        mock.get("/api/ingestion/collections/1").mock(side_effect=_collection)
        mock.post("/api/studio/job-tokens").mock(side_effect=_mint)
        mock.route(url__regex=r".*/v1/artifact").mock(side_effect=_record)

        transport = httpx.ASGITransport(app=_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/studio/slides/jobs",
                json={"collection_id": 1, "preset": "教學投影片"},
            )
            assert resp.status_code == 202, resp.text
            job_id = resp.json()["job_id"]
            rec = jobs.get_job(job_id)
            assert rec is not None and rec.task is not None
            await rec.task
            await job_lifecycle.drain()

    finished = jobs.get_job(job_id)
    assert finished is not None
    assert finished.state == "done", finished.error
    assert finished.pptx_bytes == b"PK-deck"
    assert user_reads["n"] == 1
    patches = [auth for method, path, auth in later if method == "PATCH"]
    assert patches, later
    assert patches[0] == f"Bearer {_JOB}"
    artifact_posts = [
        auth
        for method, path, auth in later
        if method == "POST" and path.rstrip("/").endswith("/v1/artifacts")
    ]
    assert artifact_posts == [f"Bearer {_JOB}"]
    jobs._reset_for_tests()
