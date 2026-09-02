"""簡報預覽（2026-09-02）：視覺 QA 已經截了每一頁的圖，存下來給前端當預覽；
沒跑 QA 的 job 第一次要看時再向渲染器要。"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.auth import CurrentUserIdentity
from app.config import settings

_ME = CurrentUserIdentity(id=7, username="u", role="user", token_version=0)
_OTHER = CurrentUserIdentity(id=8, username="v", role="user", token_version=0)


async def test_visual_qa_report_carries_the_screenshots(monkeypatch):
    from app.services import studio_vision_qa as vq

    async def fake_shots(pptx_path): return [b"png1", b"png2"]
    async def fake_geom(pptx_bytes, *, renderer_url=None, kinds=None, **kw): return []
    async def fake_inspect(bearer, idx, png): return []
    monkeypatch.setattr(vq, "_capture_screenshots", fake_shots)
    monkeypatch.setattr(vq, "run_geometric_qa", fake_geom)
    monkeypatch.setattr(vq, "_inspect_slide_visually", fake_inspect)
    out = await vq.visual_qa("t", "/tmp/pptx-out/x.pptx", pptx_bytes=b"PK")
    assert out.screenshots == [b"png1", b"png2"]


async def test_preview_endpoints_serve_persisted_pngs(monkeypatch, tmp_path):
    from app.api import studio as studio_mod
    from app.services import job_lifecycle, studio_previews
    from app.services import studio_job_service as job_mod
    monkeypatch.setattr(settings, "ARTIFACTS_DIR", str(tmp_path))
    job_mod._reset_for_tests()
    studio_previews.persist_previews("j_p", [b"\x89PNG-a", b"\x89PNG-b"])

    async def fake_read_status(job_id, user_id):
        return {"job_id": job_id, "state": "done", "title": "t"} if user_id == 7 else None
    monkeypatch.setattr(job_lifecycle, "read_status", fake_read_status)

    listing = await studio_mod.list_slide_previews("j_p", identity=_ME)
    assert listing["count"] == 2
    assert listing["slides"][1]["url"].endswith("/api/studio/slides/jobs/j_p/preview/1")
    resp = await studio_mod.get_slide_preview("j_p", 1, identity=_ME)
    assert resp.media_type == "image/png" and resp.body == b"\x89PNG-b"
    with pytest.raises(HTTPException) as exc:
        await studio_mod.get_slide_preview("j_p", 5, identity=_ME)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        await studio_mod.list_slide_previews("j_p", identity=_OTHER)
    assert exc.value.status_code == 404


async def test_previews_are_generated_on_demand_from_the_deck(monkeypatch, tmp_path):
    from app.api import studio as studio_mod
    from app.services import job_lifecycle, studio_previews
    from app.services import studio_job_service as job_mod
    monkeypatch.setattr(settings, "ARTIFACTS_DIR", str(tmp_path))
    job_mod._reset_for_tests()
    job_mod.persist_pptx("j_d", b"PK-deck")
    calls = []

    async def fake_render_shots(pptx_bytes):
        calls.append(pptx_bytes)
        return [b"\x89PNG-x"]
    monkeypatch.setattr(studio_previews, "screenshots_from_bytes", fake_render_shots)

    async def fake_read_status(job_id, user_id):
        return {"job_id": job_id, "state": "done", "title": "t"}
    monkeypatch.setattr(job_lifecycle, "read_status", fake_read_status)

    listing = await studio_mod.list_slide_previews("j_d", identity=_ME)
    assert listing["count"] == 1 and calls == [b"PK-deck"]
    assert studio_previews.count_previews("j_d") == 1  # persisted for next time
