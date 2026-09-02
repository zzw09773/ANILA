"""每張投影片的來源腳註（2026-09-02）：模型寫的 [N] 在被拿掉之前先換成檔名，
放在 Slide.source_line，渲染器印在頁底。給長官看的簡報，每頁要能回答「這是哪來的」。"""
from __future__ import annotations

from app.schemas.studio import SlidesSpec
from app.services.studio_sources import attach_source_lines

CHUNKS = [
    {"filename": "軍人懲罰法.pdf", "chunk_key": "c3"},
    {"filename": "軍人懲罰法.pdf", "chunk_key": "c5"},
    {"filename": "陸海空軍懲罰法施行細則.pdf", "chunk_key": "c8"},
]


def test_source_line_built_from_cited_chunks_before_citations_are_stripped():
    spec = SlidesSpec(title="t", slides=[
        {"title": "封面", "layout_kind": "section_break", "bullets": ["x"]},
        {"title": "權責 [1]", "bullets": ["一般懲罰由權責長官核定 [2]", "戰時 (參 [3])"]},
        {"title": "沒有引用", "bullets": ["純敘述"]},
    ])
    out = attach_source_lines(spec, CHUNKS)
    assert out.slides[1].source_line == "資料來源：軍人懲罰法.pdf、陸海空軍懲罰法施行細則.pdf"
    assert out.slides[2].source_line is None
    assert out.slides[0].source_line is None  # 封面不標


def test_source_line_survives_normalisation_and_ignores_bad_refs():
    from app.services.studio_text_normalizer import normalize_spec
    spec = SlidesSpec(title="t", slides=[
        {"title": "封面", "layout_kind": "section_break", "bullets": ["x"]},
        {"title": "x", "bullets": ["超出範圍 [99]", "有效 [1]"]},
    ])
    out = normalize_spec(attach_source_lines(spec, CHUNKS))
    assert out.slides[1].source_line == "資料來源：軍人懲罰法.pdf"
    assert out.slides[1].bullets == ["超出範圍", "有效"]


def test_source_line_is_capped():
    many = [{"filename": f"文件{i}.pdf", "chunk_key": f"k{i}"} for i in range(1, 9)]
    spec = SlidesSpec(title="t", slides=[
        {"title": "封面", "layout_kind": "section_break", "bullets": ["x"]},
        {"title": "x", "bullets": [" ".join(f"[{i}]" for i in range(1, 9))]},
    ])
    line = attach_source_lines(spec, many).slides[1].source_line
    assert line.count("、") <= 2 and "等" in line
