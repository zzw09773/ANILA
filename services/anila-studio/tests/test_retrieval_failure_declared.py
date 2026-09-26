"""Regression: a FAILED retrieval must never be dressed up as "0 hits".

The report pipeline already refuses to run when retrieval errors
(``test_report_runner.py::test_retrieval_failure_surfaces_as_runtime_error``).
The other three RAG pipelines still ship an artifact — that is the
product contract, ``skip_retrieval`` is a supported mode — so for them
the requirement is that the degradation is *declared*:

  * the LLM prompt must NOT contain the "本次未檢索到相關段落" /
    "無檢索結果" copy, which asserts a fact about the corpus that
    nobody established;
  * the job must reach ``done`` carrying ``warning`` so the SPA renders
    amber copy instead of a clean win.

Each test asserts both halves, and the "0 hits" test alongside it pins
that a genuinely empty search still gets the old (true) copy and NO
warning — otherwise a change that warns unconditionally would pass.
"""
from __future__ import annotations

import asyncio
import json
import pathlib

import httpx
import pytest
from fastapi import FastAPI

from app.auth import (
    CurrentUserIdentity,
    get_bearer_token,
    get_current_user_identity,
)
from app.clients.csp_client import ChunkHit, CollectionMeta, CspServerError
from app.schemas.studio import GenerateSpecRequest
from app.services.retrieval_status import RETRIEVAL_FAILED_WARNING


_IDENTITY = CurrentUserIdentity(
    id=42, username="alice", role="user", token_version=0,
)

# The false-premise sentences this whole module exists to keep out of a
# prompt built on top of a failed search.
_ZERO_HIT_CLAIMS = ("本次未檢索到相關段落", "無檢索結果")


def _collection(collection_id: int = 1) -> CollectionMeta:
    return CollectionMeta(
        id=collection_id,
        name="測試知識庫",
        embedding_model="dummy",
        embedding_dim=512,
        status="active",
        created_by=1,
    )


def _chunk() -> ChunkHit:
    return ChunkHit(
        chunk_id=1,
        document_id=10,
        filename="report_q4.pdf",
        chunk_key="c-0001",
        content=(
            "業績達成率 47%,較上季 +12 個百分點;全年營收 9,876,543 元,"
            "主要來自企業大宗訂單,毛利率維持在 32% 上下。"
        ),
        score=0.92,
        metadata={},
        parent_chunk_id=None,
        parent_content=None,
        chunk_type="leaf",
        chunk_level=0,
    )


@pytest.fixture
def artifacts_dir(tmp_path, monkeypatch):
    from app.config import settings as settings_mod

    monkeypatch.setattr(settings_mod, "ARTIFACTS_DIR", str(tmp_path))
    return tmp_path


# ── Slides ────────────────────────────────────────────────────────────────


_SLIDES_SPEC_JSON = json.dumps(
    {
        "title": "測試簡報",
        "theme": "corporate_navy",
        "slides": [
            {"title": "封面", "bullets": ["第一點內容", "第二點內容", "第三點"]},
            {"title": "結論", "bullets": ["結論一則", "結論兩則", "結論三則"]},
        ],
    }
)


def _patch_slides(monkeypatch, *, chunks_result, captured):
    """Wire the slide pipeline's externals.

    ``chunks_result`` is either a list (returned) or an exception
    instance (raised) — that single knob is the difference between the
    "0 hits" and the "retrieval failed" scenarios.
    """
    from app.api import studio as studio_mod

    async def fake_get_collection(collection_id, *, bearer):
        return _collection(collection_id)

    async def fake_retrieve_chunks(bearer, collection_id, seed_query):
        if isinstance(chunks_result, BaseException):
            raise chunks_result
        return chunks_result

    async def fake_retrieve_images(bearer, collection_id, seed_query):
        return []

    async def fake_call_llm_chat(bearer, model, messages, **kwargs):
        captured.append(messages)
        return _SLIDES_SPEC_JSON

    async def fake_render_pptx(spec, images_lookup, **kwargs):
        # pptx_path=None short-circuits vision QA (no VLM in unit tests).
        return b"PK-fake-pptx", None

    async def fake_active_provider():
        return None

    async def fake_rebalance(spec_dict, violations, chunks_text, *, bearer):
        return spec_dict

    monkeypatch.setattr(studio_mod, "get_collection", fake_get_collection)
    monkeypatch.setattr(studio_mod, "_retrieve_chunks", fake_retrieve_chunks)
    monkeypatch.setattr(studio_mod, "_retrieve_images", fake_retrieve_images)
    monkeypatch.setattr(studio_mod, "_call_llm_chat", fake_call_llm_chat)
    monkeypatch.setattr(studio_mod, "_render_pptx", fake_render_pptx)
    monkeypatch.setattr(studio_mod, "_rebalance_layouts", fake_rebalance)


async def _run_slides_job(monkeypatch, *, chunks_result):
    """Run one slide job through the real job service; return (status, prompt)."""
    from app.api import studio as studio_mod
    from app.services import studio_job_service as job_mod

    captured: list[list[dict]] = []
    _patch_slides(monkeypatch, chunks_result=chunks_result, captured=captured)
    job_mod._reset_for_tests()

    payload = GenerateSpecRequest(collection_id=1, preset="經典報告結構")

    async def runner(updater):
        await studio_mod._run_pipeline(
            identity=_IDENTITY, bearer="t", payload=payload, updater=updater,
        )

    rec = await job_mod.create_job(
        user_id=_IDENTITY.id, collection_id=1, runner=runner, report_ctx=None,
    )
    await job_mod.get_job(rec.job_id).task
    status = job_mod.get_job(rec.job_id).to_status()
    user_prompt = captured[0][1]["content"]
    job_mod._reset_for_tests()
    return status, user_prompt


async def test_slides_retrieval_failure_is_declared(monkeypatch, artifacts_dir):
    status, prompt = await _run_slides_job(
        monkeypatch, chunks_result=CspServerError("csp 5xx"),
    )

    assert status.state == "done"
    assert status.warning == RETRIEVAL_FAILED_WARNING
    for claim in _ZERO_HIT_CLAIMS:
        assert claim not in prompt, f"prompt still asserts {claim!r}"
    assert "檢索**失敗**" in prompt
    assert "speaker_notes" in prompt
    # The operator log may name the cause; the model and the user may not.
    assert "csp 5xx" not in prompt
    assert "csp 5xx" not in (status.warning or "")


async def test_slides_zero_hits_keeps_the_true_copy(monkeypatch, artifacts_dir):
    status, prompt = await _run_slides_job(monkeypatch, chunks_result=[])

    assert status.state == "done"
    assert status.warning is None
    assert "本次未檢索到相關段落" in prompt
    assert "檢索**失敗**" not in prompt


async def test_slides_hits_produce_no_warning(monkeypatch, artifacts_dir):
    status, prompt = await _run_slides_job(
        monkeypatch,
        chunks_result=[
            {
                "filename": "report_q4.pdf",
                "chunk_key": "c-0001",
                "content": "業績達成率 47%。",
                "score": 0.92,
            },
        ],
    )

    assert status.state == "done"
    assert status.warning is None
    assert "report_q4.pdf" in prompt
    assert "檢索**失敗**" not in prompt


# ── Datatable / Infographic: endpoint-level ───────────────────────────────
#
# httpx.AsyncClient (not TestClient) so the background pipeline task
# survives between the POST and the polls — same reasoning as
# test_infographic_endpoint.py.


async def _poll_until_terminal(client, url, timeout=10.0):
    deadline = asyncio.get_event_loop().time() + timeout
    last = None
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(url)
        assert resp.status_code == 200, resp.text
        last = resp.json()
        if last["state"] in ("done", "failed", "cancelled"):
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"job never reached a terminal state (last={last})")


def _build_client_app(router):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user_identity] = lambda: _IDENTITY
    app.dependency_overrides[get_bearer_token] = lambda: "test-bearer-token"
    return app


_DATATABLE_SPEC_JSON = json.dumps(
    {
        "title": "測試表",
        "preset": "key_figures",
        "columns": [
            {"key": "metric", "label": "指標", "dtype": "text", "align": "left"},
            {"key": "value", "label": "數值", "dtype": "number", "align": "right"},
        ],
        "rows": [
            {"cells": {"metric": "客戶數", "value": 12345}},
            {"cells": {"metric": "營收", "value": 9876543}},
        ],
        "notes": None,
    }
)


def _patch_datatable(monkeypatch, *, search_result, captured):
    from app.api import datatables as dt_mod

    async def fake_get_collection(collection_id, *, bearer):
        return _collection(collection_id)

    async def fake_search_chunks(collection_id, query, **kwargs):
        if isinstance(search_result, BaseException):
            raise search_result
        return search_result

    async def fake_proxy(*, model, messages, **kwargs):
        captured.append(messages)
        return {
            "choices": [
                {"message": {"role": "assistant", "content": _DATATABLE_SPEC_JSON}}
            ]
        }

    monkeypatch.setattr(dt_mod, "get_collection", fake_get_collection)
    monkeypatch.setattr(dt_mod, "search_chunks", fake_search_chunks)
    monkeypatch.setattr(dt_mod, "proxy_chat_completions", fake_proxy)


async def _run_datatable_job(monkeypatch, *, search_result):
    from app.api import datatables as dt_mod
    from app.services import datatable_job_service as job_mod

    captured: list[list[dict]] = []
    _patch_datatable(monkeypatch, search_result=search_result, captured=captured)
    job_mod._jobs.clear()

    app = _build_client_app(dt_mod.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver",
    ) as client:
        resp = await client.post(
            "/api/datatables/jobs",
            json={"collection_id": 1, "preset": "key_figures"},
        )
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job_id"]
        body = await _poll_until_terminal(
            client, f"/api/datatables/jobs/{job_id}",
        )
    user_prompt = captured[0][1]["content"]
    job_mod._jobs.clear()
    return body, user_prompt


async def test_datatable_retrieval_failure_is_declared(
    monkeypatch, artifacts_dir,
):
    body, prompt = await _run_datatable_job(
        monkeypatch, search_result=CspServerError("csp 5xx"),
    )

    assert body["state"] == "done"
    assert body["warning"] == RETRIEVAL_FAILED_WARNING
    for claim in _ZERO_HIT_CLAIMS:
        assert claim not in prompt, f"prompt still asserts {claim!r}"
    assert "檢索**失敗**" in prompt
    assert "csp 5xx" not in prompt
    assert "csp 5xx" not in (body["warning"] or "")


async def test_datatable_zero_hits_keeps_the_true_copy(
    monkeypatch, artifacts_dir,
):
    body, prompt = await _run_datatable_job(monkeypatch, search_result=[])

    assert body["state"] == "done"
    assert body["warning"] is None
    assert "無檢索結果" in prompt
    assert "檢索**失敗**" not in prompt


async def test_datatable_hits_produce_no_warning(monkeypatch, artifacts_dir):
    body, prompt = await _run_datatable_job(
        monkeypatch, search_result=[_chunk()],
    )

    assert body["state"] == "done"
    assert body["warning"] is None
    assert "report_q4.pdf" in prompt
    assert "檢索**失敗**" not in prompt


_INFOGRAPHIC_SPEC_JSON = json.dumps(
    {
        "title": "測試資訊圖表",
        "preset": "mission_dashboard",
        "stats": [{"value": "47%", "label": "目標達成率"}],
        "charts": [],
        "comparison": [],
        "timeline": [],
        "takeaway": "本季關鍵指標達成預期。",
    }
)


def _patch_infographic(monkeypatch, *, search_result, captured):
    from app.api import infographics as ig_mod

    async def fake_get_collection(collection_id, *, bearer):
        return _collection(collection_id)

    async def fake_search_chunks(collection_id, query, **kwargs):
        if isinstance(search_result, BaseException):
            raise search_result
        return search_result

    async def fake_proxy(*, model, messages, **kwargs):
        captured.append(messages)
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": _INFOGRAPHIC_SPEC_JSON,
                    }
                }
            ]
        }

    async def fake_render_pdf(html, dest_path):
        dest = pathlib.Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-1.4\n%fake pdf for tests\n")

    monkeypatch.setattr(ig_mod, "get_collection", fake_get_collection)
    monkeypatch.setattr(ig_mod, "search_chunks", fake_search_chunks)
    monkeypatch.setattr(ig_mod, "proxy_chat_completions", fake_proxy)
    monkeypatch.setattr(ig_mod, "render_pdf", fake_render_pdf)


async def _run_infographic_job(monkeypatch, *, search_result):
    from app.api import infographics as ig_mod
    from app.services import infographic_job_service as job_mod

    captured: list[list[dict]] = []
    _patch_infographic(
        monkeypatch, search_result=search_result, captured=captured,
    )
    job_mod._jobs.clear()

    app = _build_client_app(ig_mod.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver",
    ) as client:
        resp = await client.post(
            "/api/infographics/jobs",
            json={"collection_id": 1, "preset": "mission_dashboard"},
        )
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job_id"]
        body = await _poll_until_terminal(
            client, f"/api/infographics/jobs/{job_id}",
        )
    user_prompt = captured[0][1]["content"]
    job_mod._jobs.clear()
    return body, user_prompt


async def test_infographic_retrieval_failure_is_declared(
    monkeypatch, artifacts_dir,
):
    body, prompt = await _run_infographic_job(
        monkeypatch, search_result=CspServerError("csp 5xx"),
    )

    assert body["state"] == "done"
    assert body["warning"] == RETRIEVAL_FAILED_WARNING
    for claim in _ZERO_HIT_CLAIMS:
        assert claim not in prompt, f"prompt still asserts {claim!r}"
    assert "檢索**失敗**" in prompt
    assert "takeaway" in prompt
    assert "csp 5xx" not in prompt
    assert "csp 5xx" not in (body["warning"] or "")


async def test_infographic_zero_hits_keeps_the_true_copy(
    monkeypatch, artifacts_dir,
):
    body, prompt = await _run_infographic_job(monkeypatch, search_result=[])

    assert body["state"] == "done"
    assert body["warning"] is None
    assert "本次未檢索到相關段落" in prompt
    assert "檢索**失敗**" not in prompt


async def test_infographic_hits_produce_no_warning(monkeypatch, artifacts_dir):
    body, prompt = await _run_infographic_job(
        monkeypatch, search_result=[_chunk()],
    )

    assert body["state"] == "done"
    assert body["warning"] is None
    assert "report_q4.pdf" in prompt
    assert "檢索**失敗**" not in prompt
