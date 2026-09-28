"""視覺檢查：一張圖不能卡住 300 秒，警告要講真正的原因。"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.auth import CurrentUserIdentity
from app.schemas.studio import GenerateSpecRequest, VisualDefect
from app.services.studio_vision_qa import DefectReport


_IDENTITY = CurrentUserIdentity(id=1, username="u", role="user", token_version=0)


async def _visual(monkeypatch, exc: HTTPException) -> DefectReport:
    from app.services import studio_vision_qa as vq

    async def fake_shots(pptx_path):
        return [b"png"]

    async def fake_geom(pptx_bytes, *, renderer_url=None, kinds=None, **kw):
        return []

    async def fake_inspect(bearer, idx, png, **kw):
        raise exc

    monkeypatch.setattr(vq, "_capture_screenshots", fake_shots)
    monkeypatch.setattr(vq, "run_geometric_qa", fake_geom)
    monkeypatch.setattr(vq, "_inspect_slide_visually", fake_inspect)
    out = await vq.visual_qa("t", "/tmp/deck.pptx", pptx_bytes=b"PK")
    assert isinstance(out, DefectReport)
    return out


async def test_vision_timeout_warning_is_not_image_rejection(monkeypatch):
    out = await _visual(
        monkeypatch,
        HTTPException(status_code=504, detail="模型呼叫逾時"),
    )
    assert out.vision_skipped == "視覺檢查逾時，只做了版面幾何檢查。"


async def test_vision_auth_failure_warning_is_not_image_rejection(monkeypatch):
    out = await _visual(
        monkeypatch,
        HTTPException(status_code=401, detail="權杖已過期"),
    )
    text = out.vision_skipped or ""
    assert "授權失效" in text
    assert "不接受圖片" not in text
    assert "只做了版面幾何檢查" in text


async def test_image_rejection_warning_only_for_image_4xx(monkeypatch):
    image = await _visual(
        monkeypatch,
        HTTPException(
            status_code=502,
            detail="csp proxy failed: csp returned 400: image input not supported",
        ),
    )
    assert image.vision_skipped == (
        "視覺檢查未執行（模型不接受圖片輸入），只做了版面幾何檢查。"
    )

    other = await _visual(
        monkeypatch,
        HTTPException(status_code=502, detail="csp proxy failed: upstream 500"),
    )
    assert other.vision_skipped
    assert "不接受圖片" not in other.vision_skipped
    assert "逾時" not in other.vision_skipped
    assert "授權失效" not in other.vision_skipped


async def test_pipeline_keeps_the_timeout_warning(monkeypatch, tmp_path):
    from app.api import studio as studio_mod
    from app.clients.csp_client import CollectionMeta
    from app.config import settings
    from app.services import studio_job_service as job_mod

    monkeypatch.setattr(settings, "ARTIFACTS_DIR", str(tmp_path))

    async def fake_get_collection(cid, *, bearer):
        return CollectionMeta(
            id=1, name="kb", embedding_model="dummy", embedding_dim=8,
            status="active", created_by=1,
        )

    async def fake_chunks(bearer, cid, q):
        return []

    async def fake_images(bearer, cid, q):
        return []

    async def fake_llm(bearer, model, messages, **kw):
        return (
            '{"title":"測試簡報","theme":"official","slides":'
            '[{"title":"封面","layout_kind":"section_break","bullets":["副標"]}]}'
        )

    async def fake_render(spec, images_lookup, **kw):
        return b"PK-deck", "/tmp/pptx-out/x.pptx"

    async def fake_visual_qa(bearer, pptx_path, *, pptx_bytes=None, **kw):
        report = DefectReport()
        report.vision_skipped = "視覺檢查逾時，只做了版面幾何檢查。"
        return report

    monkeypatch.setattr(studio_mod, "get_collection", fake_get_collection)
    monkeypatch.setattr(studio_mod, "_retrieve_chunks", fake_chunks)
    monkeypatch.setattr(studio_mod, "_retrieve_images", fake_images)
    monkeypatch.setattr(studio_mod, "_call_llm_chat", fake_llm)
    monkeypatch.setattr(studio_mod, "_render_pptx", fake_render)
    monkeypatch.setattr(studio_mod, "_visual_qa", fake_visual_qa)

    job_mod._reset_for_tests()
    payload = GenerateSpecRequest(collection_id=1, preset="教學投影片")

    async def runner(updater):
        await studio_mod._run_pipeline(
            identity=_IDENTITY, bearer="t", payload=payload, updater=updater,
        )

    rec = await job_mod.create_job(
        user_id=1, collection_id=1, runner=runner, report_ctx=None,
    )
    await job_mod.get_job(rec.job_id).task
    finished = job_mod.get_job(rec.job_id)
    job_mod._reset_for_tests()
    assert finished.state == "done", finished.error
    assert finished.warning == "視覺檢查逾時，只做了版面幾何檢查。"
    assert "不接受圖片" not in (finished.warning or "")


async def test_vision_call_uses_sixty_second_timeout_unless_overridden(monkeypatch):
    from app.services import studio_llm

    seen: list[float | None] = []

    async def fake_resolve(name, sentinel):
        return name

    async def fake_proxy(**kwargs):
        seen.append(kwargs.get("timeout_seconds"))
        return {"choices": [{"message": {"content": "{\"defects\": []}"}}]}

    monkeypatch.setattr(studio_llm, "resolve_model_name", fake_resolve)
    monkeypatch.setattr(studio_llm, "proxy_chat_completions", fake_proxy)

    from app.services.studio_vision_qa import _inspect_slide_visually

    await _inspect_slide_visually("t", 0, b"\x89PNG")
    assert seen == [60.0]

    seen.clear()
    await studio_llm.call_llm_chat(
        "t",
        "vision-model",
        [{"role": "user", "content": "hi"}],
        timeout_seconds=12.5,
    )
    assert seen == [12.5]
