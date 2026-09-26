"""2026-09-02 活體走查抓到的一批缺陷，各一個守衛。

- `[N]`／`[5, 10]` 引用編號漏到投影片上（stripper 只認「(參 [N])」）
- s2twp 把「程序」改成「程式」、「項目」改成「專案」（法規用語被改壞）
- 修正階段 LLM 失敗把已渲染好的簡報整個丟掉
- 渲染器前置封面後，缺陷索引與 spec 索引差一
- 幾何 QA 沒拿到各頁版型，封面／章節頁被判留白
- 沒接 FLUX 的部署，prompt 仍教模型寫插圖提示詞
- image_focus 偽裝頁 bullets < 3 就逃過稽核
- 視覺 QA 模型不吃圖時整個 job 炸掉
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx
from fastapi import HTTPException

from app.auth import CurrentUserIdentity
from app.clients.csp_client import ChunkHit, CollectionMeta
from app.schemas.studio import GenerateSpecRequest, SlidesSpec, VisualDefect
from app.services.studio_text_normalizer import _convert, strip_inline_citations


# ── 引用編號 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, want", [
    ("權責長官核定原則 [8]", "權責長官核定原則"),
    ("違紀行為之定義 [5, 10]", "違紀行為之定義"),
    ("權益救濟管道 [6, 7, 9, 11]", "權益救濟管道"),
    ("申訴不停止執行原則 【6】", "申訴不停止執行原則"),
    ("結尾兩個 [1] [2]", "結尾兩個"),
    ("舊寫法 (參 [3])", "舊寫法"),
    ("如 [5] 所述，不動中間的", "如 [5] 所述，不動中間的"),
    ("第 3 條 [", "第 3 條 ["),
])
def test_bare_bracket_citations_are_stripped_from_slide_text(raw, want):
    assert strip_inline_citations(raw) == want


# ── 台灣用語轉換不可改壞法規用語 ─────────────────────────────────────────

@pytest.mark.parametrize("raw, want", [
    ("程序保障", "程序保障"),
    ("共同項目：撤職", "共同項目：撤職"),
    ("依相關文件辦理", "依相關文件辦理"),
    ("品質與質量並存", "品質與質量並存"),
    ("视频软件", "影片軟體"),
    ("信息網絡", "資訊網路"),
    ("鼠標與屏幕", "滑鼠與螢幕"),
    ("默認登錄", "預設登入"),
])
def test_normalizer_keeps_legal_words_and_fixes_tech_words(raw, want):
    assert _convert(raw) == want


def test_generation_prompt_no_longer_teaches_the_harmful_mappings():
    from app.services.studio_llm import build_generation_prompt
    system, _ = build_generation_prompt("kb", "教學投影片", None, [], retrieval_failed=False)
    assert "程序 → 程式" not in system
    assert "文件 → 檔案" not in system
    assert "視頻 → 影片" in system


# ── prompt 只在真的有插圖產生器時才教插圖 ───────────────────────────────

def test_prompt_gates_illustration_sections_on_provider():
    from app.services.studio_llm import build_generation_prompt
    off, _ = build_generation_prompt("kb", "教學投影片", None, [], retrieval_failed=False, illustrations_enabled=False)
    on, _ = build_generation_prompt("kb", "教學投影片", None, [], retrieval_failed=False, illustrations_enabled=True)
    assert "image_prompt" not in off and "生圖模型" not in off
    assert "diagram_dot" in off
    assert "image_prompt" in on and "生圖模型" in on
    assert "50-500" not in on
    assert "240" in on
    assert "抽象" in on


# ── 稽核：偽裝的 image_focus 不管幾條 bullet 都是候選 ─────────────────────

def test_audit_flags_disguised_image_focus_with_one_bullet():
    from app.services.studio_layout import _audit_layout_distribution
    spec = SlidesSpec(**{
        "title": "t",
        "slides": [
            {"title": "封面", "layout_kind": "section_break", "bullets": ["副標"]},
            {"title": "懲罰執行流程圖", "layout_kind": "image_focus", "image_kind": "illustration",
             "image_prompt": "a flow", "bullets": ["懲罰行政作業步驟"]},
            {"title": "內容", "layout_kind": "icon_rows", "bullets": ["a", "b", "c"],
             "icon_rows": [{"concept": "security", "heading": "h", "description": "d"}] * 3},
        ],
    })
    kinds = [v.kind for v in _audit_layout_distribution(spec, chunks_text="")]
    assert "V4_CONTENT" in kinds


# ── 索引對齊 ──────────────────────────────────────────────────────────────

def test_rendered_defect_indices_map_back_to_spec_indices():
    from app.services.studio_vision_qa import to_spec_indices
    defects = [
        VisualDefect(slide_index=0, severity="critical", summary="cover whitespace"),
        VisualDefect(slide_index=1, severity="warning", summary="first spec slide"),
        VisualDefect(slide_index=3, severity="critical", summary="third spec slide"),
    ]
    mapped = to_spec_indices(defects, cover_prepended=True)
    assert [(d.slide_index, d.summary) for d in mapped] == [(0, "first spec slide"), (2, "third spec slide")]
    same = to_spec_indices(defects, cover_prepended=False)
    assert [d.slide_index for d in same] == [0, 1, 3]


# ── 幾何 QA 帶各頁版型 ───────────────────────────────────────────────────

@respx.mock
async def test_geometric_qa_posts_rendered_kinds():
    from app.services.geometric_qa import run_geometric_qa
    route = respx.post("http://r/qa-geometric").mock(return_value=httpx.Response(200, json={"defects": []}))
    await run_geometric_qa(b"PK", renderer_url="http://r", kinds=["cover", "standard", "quote"])
    body = json.loads(route.calls[0].request.content)
    assert body["kinds"] == ["cover", "standard", "quote"]


# ── 修正階段失敗不可丟掉已渲染的簡報 ─────────────────────────────────────

_IDENTITY = CurrentUserIdentity(id=1, username="u", role="user", token_version=0)
_SPEC_JSON = json.dumps({
    "title": "測試簡報", "theme": "corporate_navy",
    "slides": [
        {"title": "封面", "layout_kind": "section_break", "bullets": ["副標"]},
        {"title": "內容", "bullets": ["第一點內容", "第二點內容", "第三點"]},
    ],
})


def _coll():
    return CollectionMeta(id=1, name="kb", embedding_model="dummy", embedding_dim=512, status="active", created_by=1)


def _patch(monkeypatch, *, fix_raises, vision_defects):
    from app.api import studio as studio_mod

    async def fake_get_collection(cid, *, bearer): return _coll()
    async def fake_chunks(bearer, cid, q): return []
    async def fake_images(bearer, cid, q): return []
    async def fake_llm(bearer, model, messages, **kw): return _SPEC_JSON
    async def fake_render(spec, images_lookup, **kw): return b"PK-deck", "/tmp/pptx-out/x.pptx"
    async def fake_provider(): return None
    async def fake_visual_qa(bearer, pptx_path, *, pptx_bytes=None, **kw): return list(vision_defects)
    async def fake_fix(bearer, spec, defects):
        if fix_raises: raise fix_raises
        return spec
    monkeypatch.setattr(studio_mod, "get_collection", fake_get_collection)
    monkeypatch.setattr(studio_mod, "_retrieve_chunks", fake_chunks)
    monkeypatch.setattr(studio_mod, "_retrieve_images", fake_images)
    monkeypatch.setattr(studio_mod, "_call_llm_chat", fake_llm)
    monkeypatch.setattr(studio_mod, "_render_pptx", fake_render)
    monkeypatch.setattr(studio_mod, "_visual_qa", fake_visual_qa)
    monkeypatch.setattr(studio_mod, "_fix_spec_with_defects", fake_fix)


async def _run(monkeypatch, **kw):
    from app.api import studio as studio_mod
    from app.services import studio_job_service as job_mod
    _patch(monkeypatch, **kw)
    job_mod._reset_for_tests()
    payload = GenerateSpecRequest(collection_id=1, preset="教學投影片")

    async def runner(updater):
        await studio_mod._run_pipeline(identity=_IDENTITY, bearer="t", payload=payload, updater=updater)

    rec = await job_mod.create_job(user_id=1, collection_id=1, runner=runner, report_ctx=None)
    await job_mod.get_job(rec.job_id).task
    out = job_mod.get_job(rec.job_id)
    job_mod._reset_for_tests()
    return out


async def test_fix_stage_timeout_ships_rendered_deck_with_warning(monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "ARTIFACTS_DIR", str(tmp_path))
    critical = [VisualDefect(slide_index=1, severity="critical", summary="文字溢出")]
    rec = await _run(monkeypatch, fix_raises=HTTPException(status_code=502, detail="csp transport error"), vision_defects=critical)
    assert rec.state == "done", rec.error
    assert rec.pptx_bytes == b"PK-deck"
    assert rec.warning and "修正" in rec.warning
    assert [d.summary for d in rec.defects] == ["文字溢出"]


async def test_fix_stage_success_is_still_clean(monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "ARTIFACTS_DIR", str(tmp_path))
    rec = await _run(monkeypatch, fix_raises=None, vision_defects=[])
    assert rec.state == "done" and rec.warning is None


# ── 視覺 QA：模型不吃圖 → 只用幾何結果，並回報 ─────────────────────────────

async def test_visual_qa_skips_vision_when_model_rejects_images(monkeypatch):
    from app.services import studio_vision_qa as vq

    async def fake_shots(pptx_path): return [b"png1", b"png2"]
    async def fake_geom(pptx_bytes, *, renderer_url=None, kinds=None, **kw): return []
    async def fake_inspect(bearer, idx, png): raise HTTPException(status_code=502, detail="upstream 400: image input not supported")
    monkeypatch.setattr(vq, "_capture_screenshots", fake_shots)
    monkeypatch.setattr(vq, "run_geometric_qa", fake_geom)
    monkeypatch.setattr(vq, "_inspect_slide_visually", fake_inspect)
    out = await vq.visual_qa("t", "/tmp/pptx-out/x.pptx", pptx_bytes=b"PK")
    assert list(out) == []
    assert getattr(out, "vision_skipped", None), "caller must be told vision QA did not run"
