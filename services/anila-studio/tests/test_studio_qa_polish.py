"""QA 收尾（2026-09-02 第四次活體）：
- 視覺模型把右下角的頁碼看成「亂碼字元」判 critical，每次都白跑一輪修正 → 這類敘述降級為 info。
- 修正後的第二輪視覺檢查只看被點名的那幾頁，不要 16 頁全部再看一次。
- 資料來源頁的段落標記 leaf-00002-第-3-條 → 「第 3 條」。
"""
from __future__ import annotations

from app.schemas.studio import VisualDefect


def test_corner_glyph_complaints_are_demoted_to_info():
    from app.services.studio_vision_qa import demote_known_false_positives
    raw = [
        VisualDefect(slide_index=2, severity="critical", summary="投影片右下角出現不明亂碼字元「唓」"),
        VisualDefect(slide_index=3, severity="critical", summary="頁碼與底部文字距離過近"),
        VisualDefect(slide_index=4, severity="critical", summary="標題文字溢出版面右側"),
    ]
    out = demote_known_false_positives(raw)
    assert [d.severity for d in out] == ["info", "info", "critical"]


def test_vision_prompt_explains_page_number_and_footer():
    from app.services.studio_vision_qa import VISION_SYSTEM_PROMPT
    assert "頁碼" in VISION_SYSTEM_PROMPT and "資料來源" in VISION_SYSTEM_PROMPT


async def test_second_pass_only_inspects_flagged_slides(monkeypatch):
    from app.services import studio_vision_qa as vq
    looked = []

    async def fake_shots(pptx_path): return [b"p0", b"p1", b"p2", b"p3"]
    async def fake_geom(pptx_bytes, *, renderer_url=None, kinds=None, **kw): return []
    async def fake_inspect(bearer, idx, png):
        looked.append(idx); return []
    monkeypatch.setattr(vq, "_capture_screenshots", fake_shots)
    monkeypatch.setattr(vq, "run_geometric_qa", fake_geom)
    monkeypatch.setattr(vq, "_inspect_slide_visually", fake_inspect)
    await vq.visual_qa("t", "/tmp/pptx-out/x.pptx", pptx_bytes=b"PK", only_slides={1, 3})
    assert sorted(looked) == [1, 3]


def test_sources_notes_show_article_numbers_not_chunk_keys():
    from app.schemas.studio import SlidesSpec
    from app.services.studio_sources import append_sources_slide
    chunks = [
        {"filename": "陸海空軍懲罰法.pdf", "chunk_key": "leaf-00002-第-3-條"},
        {"filename": "陸海空軍懲罰法.pdf", "chunk_key": "leaf-00011-第-12-條"},
        {"filename": "施行細則.pdf", "chunk_key": "c8"},
    ]
    spec = SlidesSpec(title="t", slides=[{"title": "封面", "layout_kind": "section_break", "bullets": ["x"]}, {"title": "a", "bullets": ["b"]}])
    last = append_sources_slide(spec, chunks).slides[-1]
    assert last.sources[0].note == "第 3 條、第 12 條"
    assert last.sources[1].note == "段落 c8"


# ── 修正只動被點名的頁 ──────────────────────────────────────────────────────

async def test_fix_pass_patches_only_the_flagged_slides(monkeypatch):
    import json
    from app.schemas.studio import SlidesSpec
    from app.services import studio_vision_qa as vq
    spec = SlidesSpec(title="t", slides=[
        {"title": "封面", "layout_kind": "section_break", "bullets": ["x"]},
        {"title": "對照", "layout_kind": "table", "bullets": ["x"], "table": {"columns": ["a", "b"], "rows": [["1", "2"]]}},
        {"title": "溢出的一頁", "bullets": ["很長的一條 " * 30, "第二條"]},
        {"title": "數字", "layout_kind": "stat_callout", "bullets": ["所以"], "stat": {"value": "30 日", "label": "期限", "supporting": "自處分送達次日起三十日內提出申訴，逾期不受理。"}},
    ])
    seen = {}

    async def fake_llm(bearer, model, messages, **kw):
        seen["user"] = messages[1]["content"]
        return json.dumps({"changes": [
            {"slide_index": 2, "slide": {"title": "溢出的一頁", "bullets": ["短一點", "第二條"]}},
            {"slide_index": 99, "slide": {"title": "不存在", "bullets": ["x"]}},
        ]}, ensure_ascii=False)
    monkeypatch.setattr(vq, "_call_llm_chat", fake_llm)
    defects = [VisualDefect(slide_index=2, severity="critical", summary="文字溢出版面")]
    out = await vq.fix_spec_with_defects("b", spec, defects)
    assert out.slides[2].bullets == ["短一點", "第二條"]
    assert out.slides[1] == spec.slides[1] and out.slides[3] == spec.slides[3]  # untouched
    assert "溢出的一頁" in seen["user"] and "對照" not in seen["user"]         # only flagged slides are sent
    assert len(out.slides) == 4


# ── 章節頁緊接著同名內容頁 → 章節頁多餘 ────────────────────────────────────

def test_section_break_that_repeats_the_next_title_is_dropped():
    from app.schemas.studio import SlidesSpec
    from app.services.studio_layout import drop_redundant_section_breaks
    spec = SlidesSpec(title="t", slides=[
        {"title": "封面", "layout_kind": "section_break", "bullets": ["x"]},
        {"title": "懲罰種類", "layout_kind": "section_break", "bullets": ["懲罰種類"]},
        {"title": "懲罰種類", "bullets": ["a", "b"]},
        {"title": "救濟", "layout_kind": "section_break", "bullets": ["申訴與再申訴"]},
        {"title": "救濟途徑", "bullets": ["c"]},
    ])
    out = drop_redundant_section_breaks(spec)
    assert [s.title for s in out.slides] == ["封面", "懲罰種類", "救濟", "救濟途徑"]
    assert out.slides[1].layout_kind == "standard"
