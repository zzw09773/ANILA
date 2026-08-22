"""製作／簡報 product loop: source gate, deck modes, cites, per-slide regen."""
from __future__ import annotations

import pytest

from app.schemas.studio import GenerateSpecRequest, NO_INDEXED_SOURCES, Slide
from app.services.studio_llm import _count_hint, build_generation_prompt


def test_deck_mode_presets_map_to_counts() -> None:
    assert _count_hint("詳細簡報")[1] == 12
    assert _count_hint("口講用短頁")[1] == 5
    assert _count_hint("閃電簡報")[1] == 5
    assert _count_hint("Lightning Talk")[1] == 5
    assert "一頁一句" in _count_hint("口講用短頁")[0]


def test_generate_request_accepts_sources_and_audience() -> None:
    req = GenerateSpecRequest(
        collection_id=3,
        preset="詳細簡報",
        document_ids=[11, 12],
        audience="主管",
    )
    assert req.document_ids == [11, 12]
    assert req.audience == "主管"


def test_prompt_includes_audience_and_spoken_count() -> None:
    system, user = build_generation_prompt(
        "測試庫",
        "口講用短頁",
        None,
        [{"filename": "a.pdf", "chunk_key": "c1", "content": "內容", "score": 0.9}],
        retrieval_failed=False,
        audience="院內同仁",
    )
    assert "5 張投影片" in system
    assert "聽眾：院內同仁" in user


def test_slide_keeps_citation_refs() -> None:
    slide = Slide(
        title="結論",
        bullets=["完成部署"],
        citation_refs=[1, 3],
        chunk_id="c-0001",
    )
    assert slide.citation_refs == [1, 3]
    assert slide.chunk_id == "c-0001"


@pytest.mark.asyncio
async def test_retrieve_chunks_forwards_document_ids(monkeypatch) -> None:
    from app.clients.csp_client import ChunkHit, CollectionMeta
    from app.services import studio_retrieval as retrieval

    async def fake_get_collection(collection_id, *, bearer):
        return CollectionMeta(
            id=collection_id,
            name="庫",
            embedding_model="dummy",
            embedding_dim=8,
            status="active",
            created_by=1,
        )

    captured: dict = {}

    async def fake_search(collection_id, query, **kwargs):
        captured.update(kwargs)
        return [
            ChunkHit(
                chunk_id=9,
                document_id=11,
                filename="a.pdf",
                chunk_key="p1#0",
                content="段落內容足夠長",
                score=0.88,
                metadata={"page": 2},
                parent_chunk_id=None,
                parent_content=None,
                chunk_type="leaf",
                chunk_level=0,
            )
        ]

    monkeypatch.setattr(retrieval, "get_collection", fake_get_collection)
    monkeypatch.setattr(retrieval, "search_chunks", fake_search)

    hits = await retrieval.retrieve_chunks(
        "t", 4, "主題", document_ids=[11, 12],
    )
    assert captured["document_ids"] == [11, 12]
    assert hits[0]["document_id"] == 11
    assert hits[0]["chunk_id"] == 9
    assert hits[0]["page"] == 2


@pytest.mark.asyncio
async def test_pipeline_attaches_sources_and_cites(monkeypatch) -> None:
    from app.api import studio as studio_mod
    from app.auth import CurrentUserIdentity
    from app.clients.csp_client import CollectionMeta
    from app.services import studio_job_service as job_mod

    identity = CurrentUserIdentity(
        id=7, username="bob", role="user", token_version=0,
    )

    async def fake_get_collection(collection_id, *, bearer):
        return CollectionMeta(
            id=collection_id,
            name="庫",
            embedding_model="dummy",
            embedding_dim=8,
            status="active",
            created_by=1,
        )

    async def fake_retrieve_chunks(
        bearer, collection_id, seed_query, document_ids=None,
    ):
        assert document_ids == [11]
        return [
            {
                "filename": "a.pdf",
                "chunk_key": "c-0001",
                "chunk_id": 1,
                "document_id": 11,
                "content": "完成部署 (參 [1])",
                "score": 0.9,
            }
        ]

    async def fake_retrieve_images(bearer, collection_id, seed_query):
        return []

    async def fake_call_llm_chat(bearer, model, messages, **kwargs):
        return (
            '{"title":"測試簡報","theme":"corporate_navy","slides":['
            '{"title":"封面","bullets":["重點 (參 [1])"],'
            '"speaker_notes":"見 [1]"},'
            '{"title":"結論","bullets":["下一步"],"speaker_notes":"無"}]}'
        )

    async def fake_render_pptx(spec, images_lookup, **kwargs):
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
    monkeypatch.setattr(studio_mod, "get_active_flux_provider", fake_active_provider)
    monkeypatch.setattr(studio_mod, "_rebalance_layouts", fake_rebalance)

    job_mod._reset_for_tests()
    payload = GenerateSpecRequest(
        collection_id=1,
        preset="詳細簡報",
        document_ids=[11],
        audience="主管",
    )

    async def runner(updater):
        await studio_mod._run_pipeline(
            identity=identity, bearer="t", payload=payload, updater=updater,
        )

    rec = await job_mod.create_job(
        user_id=identity.id, collection_id=1, runner=runner, report_ctx=None,
    )
    await job_mod.get_job(rec.job_id).task
    status = job_mod.get_job(rec.job_id).to_status()
    job_mod._reset_for_tests()

    assert status.state == "done"
    assert status.spec is not None
    assert status.sources[0].document_name == "a.pdf"
    assert 1 in status.spec.slides[0].citation_refs
    assert status.spec.slides[0].chunk_id == "c-0001"
    assert NO_INDEXED_SOURCES not in (status.error or "")


@pytest.mark.asyncio
async def test_skip_retrieval_still_generates(monkeypatch) -> None:
    from app.api import studio as studio_mod
    from app.auth import CurrentUserIdentity
    from app.clients.csp_client import CollectionMeta
    from app.services import studio_job_service as job_mod

    identity = CurrentUserIdentity(
        id=7, username="bob", role="user", token_version=0,
    )

    async def fake_get_collection(collection_id, *, bearer):
        return CollectionMeta(
            id=collection_id,
            name="庫",
            embedding_model="dummy",
            embedding_dim=8,
            status="active",
            created_by=1,
        )

    called = {"retrieve": False}

    async def fake_retrieve_chunks(*args, **kwargs):
        called["retrieve"] = True
        return []

    async def fake_retrieve_images(*args, **kwargs):
        return []

    async def fake_call_llm_chat(bearer, model, messages, **kwargs):
        return (
            '{"title":"略過檢索","theme":"corporate_navy","slides":['
            '{"title":"封面","bullets":["自由發揮"],"speaker_notes":"無"}]}'
        )

    async def fake_render_pptx(spec, images_lookup, **kwargs):
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
    monkeypatch.setattr(studio_mod, "get_active_flux_provider", fake_active_provider)
    monkeypatch.setattr(studio_mod, "_rebalance_layouts", fake_rebalance)

    job_mod._reset_for_tests()
    payload = GenerateSpecRequest(
        collection_id=1, preset="口講用短頁", skip_retrieval=True,
    )

    async def runner(updater):
        await studio_mod._run_pipeline(
            identity=identity, bearer="t", payload=payload, updater=updater,
        )

    rec = await job_mod.create_job(
        user_id=identity.id, collection_id=1, runner=runner, report_ctx=None,
    )
    await job_mod.get_job(rec.job_id).task
    status = job_mod.get_job(rec.job_id).to_status()
    job_mod._reset_for_tests()
    assert called["retrieve"] is False
    assert status.state == "done"
    assert status.spec is not None


@pytest.mark.asyncio
async def test_regenerate_replaces_one_slide(monkeypatch) -> None:
    from app.api import studio as studio_mod
    from app.auth import CurrentUserIdentity
    from app.clients.csp_client import CollectionMeta
    from app.schemas.studio import SlidesSpec
    from app.services import studio_job_service as job_mod

    identity = CurrentUserIdentity(
        id=7, username="bob", role="user", token_version=0,
    )

    spec = SlidesSpec.model_validate(
        {
            "title": "測試簡報",
            "theme": "corporate_navy",
            "slides": [
                {"title": "封面", "bullets": ["舊封面"]},
                {"title": "結論", "bullets": ["舊結論"]},
            ],
        }
    )

    async def runner(updater):
        await updater.mark_done(
            spec=spec,
            pptx_bytes=b"PK-old",
            defects=[],
            qa_passes=0,
            source_chunks=[
                {
                    "filename": "a.pdf",
                    "chunk_key": "c-0001",
                    "document_id": 11,
                    "content": "新論點",
                    "score": 0.9,
                }
            ],
        )

    job_mod._reset_for_tests()
    rec = await job_mod.create_job(
        user_id=identity.id, collection_id=1, runner=runner, report_ctx=None,
    )
    await job_mod.get_job(rec.job_id).task

    async def fake_get_collection(collection_id, *, bearer):
        return CollectionMeta(
            id=collection_id,
            name="庫",
            embedding_model="dummy",
            embedding_dim=8,
            status="active",
            created_by=1,
        )

    async def fake_call_llm_chat(bearer, model, messages, **kwargs):
        return '{"title":"新結論","bullets":["改寫後 (參 [1])"],"speaker_notes":"見 [1]","layout_kind":"standard"}'

    async def fake_render_pptx(next_spec, images_lookup, **kwargs):
        assert next_spec.slides[0].title == "封面"
        assert next_spec.slides[1].title == "新結論（重做）"
        return b"PK-new", None

    monkeypatch.setattr(studio_mod, "get_collection", fake_get_collection)
    monkeypatch.setattr(studio_mod, "_call_llm_chat", fake_call_llm_chat)
    monkeypatch.setattr(studio_mod, "_render_pptx", fake_render_pptx)

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app.auth import get_bearer_token, get_current_user_identity

    app = FastAPI()
    app.include_router(studio_mod.router)
    app.dependency_overrides[get_current_user_identity] = lambda: identity
    app.dependency_overrides[get_bearer_token] = lambda: "t"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/studio/slides/jobs/{rec.job_id}/slides/2/regenerate",
            json={"extra_instructions": "更短"},
        )
    job_mod._reset_for_tests()
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["spec"]["slides"][0]["title"] == "封面"
    assert body["spec"]["slides"][1]["title"] == "新結論（重做）"
    assert 1 in body["spec"]["slides"][1]["citation_refs"]
    assert body["job_id"] == rec.job_id


@pytest.mark.asyncio
async def test_regenerate_keeps_one_slide_when_llm_returns_a_deck(
    monkeypatch,
) -> None:
    from app.api import studio as studio_mod
    from app.auth import CurrentUserIdentity
    from app.clients.csp_client import CollectionMeta
    from app.schemas.studio import SlidesSpec
    from app.services import studio_job_service as job_mod

    identity = CurrentUserIdentity(
        id=7, username="bob", role="user", token_version=0,
    )
    spec = SlidesSpec.model_validate(
        {
            "title": "測試簡報",
            "theme": "corporate_navy",
            "slides": [
                {"title": "封面", "bullets": ["舊封面"]},
                {"title": "審查摘要", "bullets": ["舊摘要"]},
                {"title": "結論", "bullets": ["舊結論"]},
            ],
        }
    )

    async def runner(updater):
        await updater.mark_done(
            spec=spec, pptx_bytes=b"PK-old", defects=[], qa_passes=0,
            source_chunks=[],
        )

    job_mod._reset_for_tests()
    rec = await job_mod.create_job(
        user_id=identity.id, collection_id=1, runner=runner, report_ctx=None,
    )
    await job_mod.get_job(rec.job_id).task

    async def fake_get_collection(collection_id, *, bearer):
        return CollectionMeta(
            id=collection_id, name="庫", embedding_model="dummy",
            embedding_dim=8, status="active", created_by=1,
        )

    async def fake_call_llm_chat(bearer, model, messages, **kwargs):
        return (
            '{"title":"全新簡報","slides":['
            '{"title":"新封面","bullets":["不該整份重做"]},'
            '{"title":"審查摘要","bullets":["新論點 (參 [1])"]},'
            '{"title":"新結論","bullets":["也不該換"]}'
            ']}'
        )

    async def fake_render_pptx(next_spec, images_lookup, **kwargs):
        assert kwargs.get("llm") is None
        assert kwargs.get("deck_base_seed") is None
        assert next_spec.slides[0].title == "封面"
        assert next_spec.slides[1].title == "審查摘要（重做）"
        assert next_spec.slides[1].bullets[0].startswith("新論點")
        assert next_spec.slides[2].title == "結論"
        return b"PK-new", None

    monkeypatch.setattr(studio_mod, "get_collection", fake_get_collection)
    monkeypatch.setattr(studio_mod, "_call_llm_chat", fake_call_llm_chat)
    monkeypatch.setattr(studio_mod, "_render_pptx", fake_render_pptx)

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app.auth import get_bearer_token, get_current_user_identity

    app = FastAPI()
    app.include_router(studio_mod.router)
    app.dependency_overrides[get_current_user_identity] = lambda: identity
    app.dependency_overrides[get_bearer_token] = lambda: "t"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/studio/slides/jobs/{rec.job_id}/slides/2/regenerate",
            json={},
        )
    job_mod._reset_for_tests()
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_id"] == rec.job_id
    assert [s["title"] for s in body["spec"]["slides"]] == [
        "封面",
        "審查摘要（重做）",
        "結論",
    ]


def test_coerce_regenerated_slide_dict_marks_title() -> None:
    from app.api.studio import _coerce_regenerated_slide_dict
    from app.schemas.studio import Slide

    current = Slide.model_validate({"title": "審查摘要", "bullets": ["舊"]})
    one = _coerce_regenerated_slide_dict(
        {"title": "審查摘要", "bullets": ["新"]},
        current=current,
        slide_index=1,
    )
    assert one["title"] == "審查摘要（重做）"
    deck = _coerce_regenerated_slide_dict(
        {
            "title": "全新簡報",
            "slides": [
                {"title": "A", "bullets": ["x"]},
                {"title": "審查摘要", "bullets": ["改寫"]},
            ],
        },
        current=current,
        slide_index=1,
    )
    assert deck["title"] == "審查摘要（重做）"
    assert deck["bullets"] == ["改寫"]
    rewritten_first = _coerce_regenerated_slide_dict(
        {
            "title": "全新簡報",
            "slides": [
                {"title": "審查摘要", "bullets": ["改寫放在第一頁"]},
                {"title": "其他", "bullets": ["不是這一頁"]},
            ],
        },
        current=current,
        slide_index=1,
    )
    assert rewritten_first["bullets"] == ["改寫放在第一頁"]
