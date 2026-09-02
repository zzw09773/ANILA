"""「言之有物、不密密麻麻」（2026-09-02 擁有者）：
- 每張內容頁一句 key_message（結論），渲染成頁底的重點列；
- 條列超過 5 條就拆頁，不擠；
- 圖：timeline / org 兩種由渲染器照資料畫，svg 由模型畫、先消毒；
- 目錄頁由大綱自動產生。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.studio import SlidesSpec


def _spec(*slides):
    return SlidesSpec(title="t", slides=[{"title": "封面", "layout_kind": "section_break", "bullets": ["x"]}, *slides])


def test_key_message_and_figure_payloads_validate():
    s = _spec({"title": "a", "bullets": ["b"], "key_message": "申訴要在三十日內提出，逾期不受理。"})
    assert s.slides[1].key_message.startswith("申訴")
    tl = _spec({"title": "時程", "layout_kind": "figure", "bullets": ["x"],
                "figure": {"kind": "timeline", "items": [{"label": "送達", "note": "第 0 日"}, {"label": "申訴", "note": "30 日內"}]}})
    assert tl.slides[1].figure.kind == "timeline" and len(tl.slides[1].figure.items) == 2
    org = _spec({"title": "組織", "layout_kind": "figure", "bullets": ["x"],
                 "figure": {"kind": "org", "items": [{"label": "國防部"}, {"label": "服役機關", "parent": "國防部"}]}})
    assert org.slides[1].figure.items[1].parent == "國防部"
    svg = _spec({"title": "架構", "layout_kind": "figure", "bullets": ["x"],
                 "figure": {"kind": "svg", "svg": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 675"><rect width="10" height="10"/></svg>'}})
    assert svg.slides[1].figure.svg.startswith("<svg")
    with pytest.raises(ValidationError):
        _spec({"title": "x", "layout_kind": "figure", "bullets": ["x"], "figure": {"kind": "timeline", "items": [{"label": "只有一點"}]}})


def test_figure_without_payload_demotes_and_svg_is_sanitised():
    s = _spec({"title": "x", "layout_kind": "figure", "bullets": ["x"]})
    assert s.slides[1].layout_kind == "standard"
    from app.services.studio_svg import sanitize_svg
    bad = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10" onload="x()"><script>alert(1)</script><a href="http://evil"><rect width="1" height="1"/></a><image href="http://evil/a.png"/><foreignObject/></svg>'
    clean = sanitize_svg(bad)
    assert clean is not None
    for token in ("script", "onload", "http://evil", "foreignObject", "<image"):
        assert token not in clean
    assert sanitize_svg("<div>not svg</div>") is None
    assert sanitize_svg("<svg xmlns='http://www.w3.org/2000/svg'><rect/></svg>") is not None  # viewBox added
    assert sanitize_svg("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1 1'>" + "<rect/>" * 20000 + "</svg>") is None  # too big


def test_dense_bullet_slides_are_split():
    from app.services.studio_layout import split_dense_slides
    spec = _spec({"title": "很多條", "bullets": [f"第 {i} 點的內容說明" for i in range(1, 9)], "key_message": "結論"})
    out = split_dense_slides(spec, max_bullets=5)
    assert [s.title for s in out.slides] == ["封面", "很多條", "很多條（續）"]
    assert len(out.slides[1].bullets) == 4 and len(out.slides[2].bullets) == 4
    assert out.slides[1].key_message == "結論" and out.slides[2].key_message is None
    untouched = split_dense_slides(_spec({"title": "剛好", "bullets": ["a", "b", "c", "d", "e"]}), max_bullets=5)
    assert len(untouched.slides) == 2


def test_agenda_slide_is_built_from_the_outline():
    from app.services.studio_outline import parse_outline
    from app.services.studio_sources import insert_agenda_slide
    import json
    outline = parse_outline(json.dumps({"title": "t", "sections": [
        {"heading": "懲罰種類", "slides": [{"title": "a", "evidence": "list", "query": "q"}]},
        {"heading": "權責", "slides": [{"title": "b", "evidence": "list", "query": "q"}]},
        {"heading": "救濟", "slides": [{"title": "c", "evidence": "list", "query": "q"}]},
    ]}, ensure_ascii=False))
    spec = _spec({"title": "a", "bullets": ["x"]})
    out = insert_agenda_slide(spec, outline)
    assert out.slides[1].layout_kind == "agenda" and out.slides[1].agenda == ["懲罰種類", "權責", "救濟"]
    assert out.slides[0].layout_kind == "section_break"  # cover stays first
    assert len(insert_agenda_slide(out, outline).slides) == len(out.slides)  # idempotent


def test_normalizer_and_source_lines_reach_key_message_and_figure_labels():
    from app.services.studio_text_normalizer import normalize_spec
    s = _spec({"title": "x", "bullets": ["b"], "key_message": "登录后查看信息 [2]",
               "layout_kind": "figure", "figure": {"kind": "timeline", "items": [{"label": "软件 [1]", "note": "视频"}, {"label": "b"}]}})
    out = normalize_spec(s)
    assert out.slides[1].key_message == "登入後查看資訊"
    assert out.slides[1].figure.items[0].label == "軟體" and out.slides[1].figure.items[0].note == "影片"


def test_content_prompt_carries_the_substance_rules():
    from app.services.studio_llm import build_generation_prompt
    system, _ = build_generation_prompt("kb", "教學投影片", None, [], retrieval_failed=False)
    assert "key_message" in system
    assert '"figure"' in system and "timeline" in system and "svg" in system
    assert "每條" in system and "字" in system  # per-bullet length rule
