"""Batch 3（2026-09-02）：兩段式產生 — 先出大綱（每張投影片說明「要證明什麼、證據型態、
檢索問句」），每張各自檢索，再逐張寫內容。內容太薄的根本原因是整份簡報只查一次。"""
from __future__ import annotations

import json

import pytest

from app.services import studio_outline as so


OUTLINE_JSON = json.dumps({
    "title": "軍人懲罰法實務教學",
    "theme": "corporate_navy",
    "sections": [
        {"heading": "懲罰種類", "slides": [
            {"title": "懲罰種類對照", "evidence": "table", "query": "軍人懲罰法 懲罰種類 軍官 士官 士兵"},
            {"title": "重大懲罰門檻", "evidence": "number", "query": "重大懲罰 二個月本俸"},
        ]},
        {"heading": "救濟", "slides": [
            {"title": "救濟程序", "evidence": "process", "query": "申訴 再申訴 行政訴訟 程序"},
        ]},
    ],
}, ensure_ascii=False)


def test_parse_outline_validates_and_normalises_evidence():
    outline = so.parse_outline(OUTLINE_JSON)
    assert outline.title == "軍人懲罰法實務教學"
    assert [s.title for s in outline.all_slides()] == ["懲罰種類對照", "重大懲罰門檻", "救濟程序"]
    assert [s.evidence for s in outline.all_slides()] == ["table", "number", "process"]
    weird = so.parse_outline(json.dumps({"title": "t", "sections": [{"heading": "h", "slides": [{"title": "x", "evidence": "PIE CHART", "query": "q"}]}]}))
    assert weird.all_slides()[0].evidence == "list"  # unknown → list
    with pytest.raises(ValueError):
        so.parse_outline(json.dumps({"title": "t", "sections": []}))


async def test_gather_slide_chunks_retrieves_per_slide_and_dedupes():
    outline = so.parse_outline(OUTLINE_JSON)
    seed = [{"filename": "law.pdf", "chunk_key": "c1", "content": "seed", "score": 0.9}]
    calls = []

    async def fake_retrieve(bearer, collection_id, query, *, top_k):
        calls.append((query, top_k))
        return [
            {"filename": "law.pdf", "chunk_key": "c1", "content": "seed again", "score": 0.8},
            {"filename": "law.pdf", "chunk_key": f"k-{len(calls)}", "content": f"hit for {query}", "score": 0.7},
        ]

    chunks, per_slide = await so.gather_slide_chunks("b", 2, outline, seed, retrieve=fake_retrieve, per_slide_k=4)
    assert [q for q, _ in calls] == ["軍人懲罰法 懲罰種類 軍官 士官 士兵", "重大懲罰 二個月本俸", "申訴 再申訴 行政訴訟 程序"]
    assert all(k == 4 for _, k in calls)
    keys = [c["chunk_key"] for c in chunks]
    assert keys[0] == "c1" and len(keys) == len(set(keys)) == 4
    # per-slide refs are 1-based [N] indices into `chunks`
    assert per_slide[0] == [1, 2] and per_slide[2] == [1, 4]


def test_content_prompt_lists_each_slide_with_its_own_chunks():
    outline = so.parse_outline(OUTLINE_JSON)
    chunks = [{"filename": "law.pdf", "chunk_key": f"c{i}", "content": f"內容 {i}", "score": 0.5} for i in range(1, 5)]
    user = so.build_content_user_prompt("軍人法規", "教學投影片", None, outline, chunks, per_slide=[[1, 2], [3], [1, 4]])
    assert "投影片 1：懲罰種類對照" in user and "table" in user
    assert "可用段落：[1] [2]" in user and "可用段落：[3]" in user
    assert "[4] 來源：law.pdf" in user


async def test_generate_two_pass_uses_outline_then_content(monkeypatch):
    from app.api import studio as studio_mod
    calls = []
    spec_json = json.dumps({"title": "t", "slides": [
        {"title": "封面", "layout_kind": "section_break", "bullets": ["x"]},
        {"title": "懲罰種類對照", "layout_kind": "table", "bullets": ["x"], "table": {"columns": ["a", "b"], "rows": [["1", "2"]]}},
    ]}, ensure_ascii=False)

    async def fake_llm(bearer, model, messages, **kw):
        calls.append(messages)
        return OUTLINE_JSON if len(calls) == 1 else spec_json

    async def fake_retrieve(bearer, collection_id, query, *, top_k=4):
        return [{"filename": "law.pdf", "chunk_key": f"k-{query[:3]}", "content": "hit", "score": 0.6}]

    monkeypatch.setattr(studio_mod, "_call_llm_chat", fake_llm)
    monkeypatch.setattr(studio_mod, "_retrieve_chunks", fake_retrieve)
    seed = [{"filename": "law.pdf", "chunk_key": "c1", "content": "seed", "score": 0.9}]
    spec, fallback = await studio_mod._generate_validated_spec(
        "b", "軍人法規", "教學投影片", None, seed, retrieval_failed=False,
        two_pass={"collection_id": 2},
    )
    assert not fallback and spec.slides[1].layout_kind == "table"
    assert len(calls) == 2
    assert "大綱" in calls[0][0]["content"]                       # pass 1 asks for an outline
    assert "投影片 1：懲罰種類對照" in calls[1][1]["content"]     # pass 2 writes from the outline


async def test_generate_falls_back_to_single_pass_when_outline_is_junk(monkeypatch):
    from app.api import studio as studio_mod
    calls = []
    spec_json = json.dumps({"title": "t", "slides": [{"title": "封面", "layout_kind": "section_break", "bullets": ["x"]}, {"title": "a", "bullets": ["b"]}]})

    async def fake_llm(bearer, model, messages, **kw):
        calls.append(messages)
        return "not json at all" if len(calls) == 1 else spec_json

    async def fake_retrieve(bearer, collection_id, query, *, top_k=4):
        raise AssertionError("no per-slide retrieval without an outline")

    monkeypatch.setattr(studio_mod, "_call_llm_chat", fake_llm)
    monkeypatch.setattr(studio_mod, "_retrieve_chunks", fake_retrieve)
    seed = [{"filename": "law.pdf", "chunk_key": "c1", "content": "seed", "score": 0.9}]
    spec, fallback = await studio_mod._generate_validated_spec(
        "b", "軍人法規", "教學投影片", None, seed, retrieval_failed=False, two_pass={"collection_id": 2},
    )
    assert not fallback and len(spec.slides) == 2
    assert len(calls) == 2 and "以下是從知識庫檢索到的相關段落" in calls[1][1]["content"]
