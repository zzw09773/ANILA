"""Batch 3（2026-09-02）：法規簡報真正需要的版型 — process（流程步驟）、table（原生表格）、
sources（結尾資料來源，由管線從檢索段落產生，不是模型寫的）。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.studio import LAYOUT_KINDS, SlidesSpec
from app.services.studio_text_normalizer import normalize_spec


def _spec(**slide):
    return SlidesSpec(title="t", slides=[{"title": "封面", "layout_kind": "section_break", "bullets": ["x"]}, slide])


def test_schema_accepts_process_table_sources():
    assert {"process", "table", "sources"} <= set(LAYOUT_KINDS)
    s = _spec(title="救濟程序", layout_kind="process", bullets=["x"],
              steps=[{"heading": "申訴", "description": "向管轄機關提出"}, {"heading": "再申訴", "description": "權保會"}])
    assert s.slides[1].layout_kind == "process" and len(s.slides[1].steps) == 2
    t = _spec(title="對照", layout_kind="table", bullets=["x"],
              table={"columns": ["身分", "懲罰"], "rows": [["軍官", "撤職"], ["士兵", "罰站"]]})
    assert t.slides[1].table.columns == ["身分", "懲罰"]
    src = _spec(title="資料來源", layout_kind="sources", bullets=["x"],
                sources=[{"label": "軍人懲罰法.pdf", "note": "chunk 3, 5"}])
    assert src.slides[1].sources[0].label == "軍人懲罰法.pdf"


def test_process_needs_at_least_two_steps_and_table_two_columns():
    with pytest.raises(ValidationError):
        _spec(title="x", layout_kind="process", bullets=["x"], steps=[{"heading": "只有一步", "description": "d"}])
    with pytest.raises(ValidationError):
        _spec(title="x", layout_kind="table", bullets=["x"], table={"columns": ["單欄"], "rows": [["a"]]})


def test_normalizer_reaches_the_new_payloads():
    s = _spec(title="流程", layout_kind="process", bullets=["x"],
              steps=[{"heading": "登录系统 [3]", "description": "查看信息"}, {"heading": "b", "description": "c"}])
    out = normalize_spec(s)
    assert out.slides[1].steps[0].heading == "登入系統"
    assert out.slides[1].steps[0].description == "查看資訊"
    t = _spec(title="表", layout_kind="table", bullets=["x"],
              table={"columns": ["身份", "软件"], "rows": [["军官", "视频 [2]"]]})
    out = normalize_spec(t)
    assert out.slides[1].table.columns == ["身份", "軟體"]
    assert out.slides[1].table.rows[0] == ["軍官", "影片"]


def test_rebalance_payload_fields_know_the_new_layouts():
    from app.services.studio_layout import _LAYOUT_PAYLOAD_FIELDS
    assert _LAYOUT_PAYLOAD_FIELDS["process"] == "steps"
    assert _LAYOUT_PAYLOAD_FIELDS["table"] == "table"


def test_sources_slide_is_appended_from_retrieved_chunks():
    from app.services.studio_sources import append_sources_slide
    chunks = [
        {"filename": "軍人懲罰法.pdf", "chunk_key": "c3", "content": "..."},
        {"filename": "軍人懲罰法.pdf", "chunk_key": "c5", "content": "..."},
        {"filename": "施行細則.pdf", "chunk_key": "c8", "content": "..."},
    ]
    spec = _spec(title="內容", layout_kind="standard", bullets=["a", "b"])
    out = append_sources_slide(spec, chunks)
    last = out.slides[-1]
    assert last.layout_kind == "sources"
    assert [s.label for s in last.sources] == ["軍人懲罰法.pdf", "施行細則.pdf"]
    assert "c3" in last.sources[0].note and "c5" in last.sources[0].note
    assert len(append_sources_slide(out, chunks).slides) == len(out.slides)  # idempotent
    assert append_sources_slide(spec, []) is spec  # nothing retrieved → nothing appended


def test_prompt_teaches_process_and_table():
    from app.services.studio_llm import build_generation_prompt
    system, _ = build_generation_prompt("kb", "教學投影片", None, [], retrieval_failed=False)
    assert '"process"' in system and "steps" in system
    assert '"table"' in system and "columns" in system and "rows" in system
